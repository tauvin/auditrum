"""Property-based tests for the core value objects and generators.

Complementary to the example-based suites: these prove invariants hold
for every valid input rather than for hand-picked cases. Four blocks,
one per ROADMAP 0.4 item:

* identifier-regex fuzz against :func:`validate_identifier`
* :class:`FieldFilter` ``only`` / ``exclude`` combinatorics
* :meth:`TrackSpec.build` checksum stability
* render → parse round-trip via pglast to confirm the emitted SQL is
  syntactically valid PostgreSQL, not just string-template output
"""

from __future__ import annotations

import re

import pglast
import pytest
from hypothesis import given
from hypothesis import strategies as st

from auditrum.tracking.spec import FieldFilter, TrackSpec, validate_identifier
from auditrum.triggers import generate_trigger_sql

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Identifiers that match our validator AND stay clear of PostgreSQL
# reserved keywords. We achieve the latter via a fixed ``t_`` prefix
# rather than a keyword blocklist — the prefix is not a PG keyword in
# any position, so every produced identifier is safe to paste straight
# into DDL. Loses coverage of short/upper/leading-underscore names;
# those are covered by the identifier fuzz block below which tests the
# regex boundary directly, without going through DDL rendering.
safe_idents = st.from_regex(
    r"^[a-z][a-z0-9_]{0,20}$", fullmatch=True
).map(lambda s: f"t_{s}")

# Every string matching the validator's regex, any case, up to PG's
# NAMEDATALEN ceiling minus a safety margin.
regex_idents = st.from_regex(_IDENT_RE, fullmatch=True).filter(
    lambda s: 1 <= len(s) <= 60
)

# Strings that should NOT match the validator regex.
invalid_ident_strings = st.text(min_size=0, max_size=30).filter(
    lambda s: not _IDENT_RE.match(s)
)


class TestValidateIdentifierFuzz:
    @given(ident=regex_idents)
    def test_accepts_every_regex_match(self, ident: str) -> None:
        assert validate_identifier(ident, "test") == ident

    @given(ident=invalid_ident_strings)
    def test_rejects_every_non_match(self, ident: str) -> None:
        with pytest.raises(ValueError):
            validate_identifier(ident, "test")

    @given(val=st.one_of(st.integers(), st.binary(), st.none()))
    def test_rejects_non_string_types(self, val: object) -> None:
        with pytest.raises(ValueError):
            validate_identifier(val, "test")  # type: ignore[arg-type]


class TestFieldFilterCombinatorics:
    @given(fields=st.lists(safe_idents, min_size=1, max_size=6, unique=True))
    def test_only_preserves_field_tuple(self, fields: list[str]) -> None:
        f = FieldFilter.only(*fields)
        assert f.kind == "only"
        assert f.fields == tuple(fields)

    @given(fields=st.lists(safe_idents, min_size=1, max_size=6, unique=True))
    def test_exclude_preserves_field_tuple(self, fields: list[str]) -> None:
        f = FieldFilter.exclude(*fields)
        assert f.kind == "exclude"
        assert f.fields == tuple(fields)

    def test_only_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            FieldFilter.only()

    def test_exclude_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            FieldFilter.exclude()

    @given(fields=st.lists(safe_idents, min_size=1, max_size=5, unique=True))
    def test_exclude_expr_mentions_every_field(self, fields: list[str]) -> None:
        expr = FieldFilter.exclude(*fields).to_ignored_keys_expr()
        for name in fields:
            assert f"'{name}'" in expr

    @given(fields=st.lists(safe_idents, min_size=1, max_size=5, unique=True))
    def test_only_expr_mentions_every_field(self, fields: list[str]) -> None:
        expr = FieldFilter.only(*fields).to_ignored_keys_expr()
        for name in fields:
            assert f"'{name}'" in expr


class TestTrackSpecChecksumStability:
    @given(
        table=safe_idents,
        audit_table=safe_idents,
        extras=st.lists(safe_idents, min_size=0, max_size=4, unique=True),
    )
    def test_same_spec_yields_same_checksum(
        self, table: str, audit_table: str, extras: list[str]
    ) -> None:
        a = TrackSpec(
            table=table,
            audit_table=audit_table,
            extra_meta_fields=tuple(extras),
        ).build()
        b = TrackSpec(
            table=table,
            audit_table=audit_table,
            extra_meta_fields=tuple(extras),
        ).build()
        assert a.checksum == b.checksum
        assert a.install_sql == b.install_sql

    @given(table=safe_idents, audit_table=safe_idents)
    def test_kwarg_order_is_irrelevant(
        self, table: str, audit_table: str
    ) -> None:
        a = TrackSpec(table=table, audit_table=audit_table).build()
        b = TrackSpec(audit_table=audit_table, table=table).build()
        assert a.checksum == b.checksum

    @given(
        table=safe_idents,
        audit_a=safe_idents,
        audit_b=safe_idents,
    )
    def test_different_audit_tables_yield_different_checksums(
        self, table: str, audit_a: str, audit_b: str
    ) -> None:
        # Documented behaviour: audit_table is a meaningful part of the
        # spec identity. If this ever becomes false the tracking table's
        # drift detection would mis-classify a retarget as no-op.
        if audit_a == audit_b:
            return
        ca = TrackSpec(table=table, audit_table=audit_a).build().checksum
        cb = TrackSpec(table=table, audit_table=audit_b).build().checksum
        assert ca != cb

    @given(fields=st.lists(safe_idents, min_size=2, max_size=5, unique=True))
    def test_field_order_in_only_affects_checksum(
        self, fields: list[str]
    ) -> None:
        # Intentional — FieldFilter.fields is a tuple, not a set.
        # Documented by this test so a future "let's sort fields for the
        # user" refactor trips on purpose rather than silently
        # invalidating every stored checksum in bidwise / catalog.
        asc = sorted(fields)
        desc = list(reversed(asc))
        if asc == desc:
            return
        ca = TrackSpec(
            table="t_a", fields=FieldFilter.only(*asc)
        ).build().checksum
        cb = TrackSpec(
            table="t_a", fields=FieldFilter.only(*desc)
        ).build().checksum
        assert ca != cb


class TestTriggerSqlRoundTrip:
    """Every code path through generate_trigger_sql must produce SQL
    that pglast parses. Catches template regressions that slip past
    string-level assertions (missing semicolons, mismatched quotes,
    unbalanced DO $$…$$ blocks) but stay invisible to unit tests."""

    @given(table=safe_idents, audit_table=safe_idents)
    def test_basic_parses(self, table: str, audit_table: str) -> None:
        pglast.parse_sql(
            generate_trigger_sql(table, audit_table=audit_table)
        )

    @given(
        table=safe_idents,
        audit_table=safe_idents,
        track_only=st.lists(safe_idents, min_size=1, max_size=5, unique=True),
    )
    def test_track_only_parses(
        self, table: str, audit_table: str, track_only: list[str]
    ) -> None:
        pglast.parse_sql(
            generate_trigger_sql(
                table, audit_table=audit_table, track_only=track_only
            )
        )

    @given(
        table=safe_idents,
        audit_table=safe_idents,
        exclude_fields=st.lists(
            safe_idents, min_size=1, max_size=5, unique=True
        ),
    )
    def test_exclude_parses(
        self,
        table: str,
        audit_table: str,
        exclude_fields: list[str],
    ) -> None:
        pglast.parse_sql(
            generate_trigger_sql(
                table,
                audit_table=audit_table,
                exclude_fields=exclude_fields,
            )
        )

    @given(
        table=safe_idents,
        audit_table=safe_idents,
        extra_meta=st.lists(safe_idents, min_size=1, max_size=3, unique=True),
    )
    def test_extra_meta_fields_parse(
        self,
        table: str,
        audit_table: str,
        extra_meta: list[str],
    ) -> None:
        pglast.parse_sql(
            generate_trigger_sql(
                table,
                audit_table=audit_table,
                extra_meta_fields=extra_meta,
            )
        )
