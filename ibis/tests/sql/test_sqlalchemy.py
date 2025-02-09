# Copyright 2015 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import operator
import unittest

import pytest
import sqlalchemy.sql as sql
from sqlalchemy import func as F
from sqlalchemy import types as sat

import ibis
import ibis.expr.datatypes as dt
from ibis.backends.base.sql.alchemy import (
    AlchemyCompiler,
    AlchemyContext,
    AlchemyExprTranslator,
    schema_from_table,
)
from ibis.tests.expr.mocks import MockAlchemyBackend
from ibis.tests.sql.test_compiler import ExprTestCases
from ibis.tests.util import assert_equal

sa = pytest.importorskip('sqlalchemy')


L = sa.literal


def _table_wrapper(name, tname=None):
    @property
    def f(self):
        t = self._table_from_schema(name, tname)
        return t

    return f


class TestSQLAlchemySelect(unittest.TestCase, ExprTestCases):
    def setUp(self):
        self.con = MockAlchemyBackend()
        self.alltypes = self.con.table('functional_alltypes')
        self.sa_alltypes = self.con.meta.tables['functional_alltypes']
        self.meta = sa.MetaData()

        self.sa_star1 = self._get_sqla('star1')

    foo = _table_wrapper('foo')
    bar = _table_wrapper('bar')
    t1 = _table_wrapper('t1', 'foo')
    t2 = _table_wrapper('t2', 'bar')

    def _table_from_schema(self, name, tname=None):
        tname = tname or name
        schema = ibis.schema(self._schemas[name])
        return self.con._inject_table(tname, schema)

    def _get_sqla(self, name):
        return self._to_sqla(self.con.table(name))

    def _check_expr_cases(self, cases, named=False):
        for expr, expected in cases:
            result = self._translate(expr, named=named)
            assert str(result) == str(expected)
            if named:
                assert result.name == expected.name

    def _translate(self, expr, named=False):
        context = AlchemyContext(compiler=self.con.compiler)
        translator = AlchemyExprTranslator(expr, context=context, named=named)
        return translator.get_result()

    def test_sqla_schema_conversion(self):
        typespec = [
            # name, type, nullable
            ('smallint', sat.SmallInteger, False, dt.int16),
            ('int', sat.Integer, True, dt.int32),
            ('integer', sat.INTEGER(), True, dt.int32),
            ('bigint', sat.BigInteger, False, dt.int64),
            ('real', sat.REAL, True, dt.float),
            ('bool', sat.Boolean, True, dt.boolean),
            ('timestamp', sat.DateTime, True, dt.timestamp),
        ]

        sqla_types = []
        ibis_types = []
        for name, t, nullable, ibis_type in typespec:
            sqla_type = sa.Column(name, t, nullable=nullable)
            sqla_types.append(sqla_type)
            ibis_types.append((name, ibis_type(nullable=nullable)))

        table = sa.Table('tname', self.meta, *sqla_types)

        schema = schema_from_table(table)
        expected = ibis.schema(ibis_types)

        assert_equal(schema, expected)

    @pytest.mark.xfail(raises=AssertionError, reason='NYT')
    def test_ibis_to_sqla_conversion(self):
        assert False

    def test_comparisons(self):
        sat = self.sa_alltypes

        ops = ['ge', 'gt', 'lt', 'le', 'eq', 'ne']

        cases = []

        for op in ops:
            f = getattr(operator, op)
            case = f(self.alltypes.double_col, 5), f(sat.c.double_col, L(5))
            cases.append(case)

        self._check_expr_cases(cases)

    def test_boolean_conjunction(self):
        sat = self.sa_alltypes
        sd = sat.c.double_col

        d = self.alltypes.double_col
        cases = [
            ((d > 0) & (d < 5), sql.and_(sd > L(0), sd < L(5))),
            ((d < 0) | (d > 5), sql.or_(sd < L(0), sd > L(5))),
        ]

        self._check_expr_cases(cases)

    def test_between(self):
        sat = self.sa_alltypes
        sd = sat.c.double_col
        d = self.alltypes.double_col

        cases = [(d.between(5, 10), sd.between(L(5), L(10)))]
        self._check_expr_cases(cases)

    def test_isnull_notnull(self):
        sat = self.sa_alltypes
        sd = sat.c.double_col
        d = self.alltypes.double_col

        cases = [
            (d.isnull(), sd.is_(sa.null())),
            (d.notnull(), sd.isnot(sa.null())),
        ]
        self._check_expr_cases(cases)

    def test_negate(self):
        sat = self.sa_alltypes
        sd = sat.c.double_col
        d = self.alltypes.double_col
        cases = [(-(d > 0), sql.not_(sd > L(0)))]

        self._check_expr_cases(cases)

    def test_coalesce(self):
        sat = self.sa_alltypes
        sd = sat.c.double_col
        sf = sat.c.float_col

        d = self.alltypes.double_col
        f = self.alltypes.float_col
        null = sa.null()

        v1 = ibis.NA
        v2 = (d > 30).ifelse(d, ibis.NA)
        v3 = f

        cases = [
            (
                ibis.coalesce(v2, v1, v3),
                sa.func.coalesce(
                    sa.case([(sd > L(30), sd)], else_=null), null, sf
                ),
            )
        ]
        self._check_expr_cases(cases)

    def test_named_expr(self):
        sat = self.sa_alltypes
        d = self.alltypes.double_col

        cases = [((d * 2).name('foo'), (sat.c.double_col * L(2)).label('foo'))]
        self._check_expr_cases(cases, named=True)

    def test_joins(self):
        region = self.con.table('tpch_region')
        nation = self.con.table('tpch_nation')

        rt = self._to_sqla(region).alias('t0')
        nt = self._to_sqla(nation).alias('t1')

        ipred = region.r_regionkey == nation.n_regionkey
        spred = rt.c.r_regionkey == nt.c.n_regionkey

        fully_mat_joins = [
            (region.inner_join(nation, ipred), rt.join(nt, spred)),
            (
                region.left_join(nation, ipred),
                rt.join(nt, spred, isouter=True),
            ),
            (
                region.outer_join(nation, ipred),
                rt.outerjoin(nt, spred, full=True),
            ),
        ]
        for ibis_joined, joined_sqla in fully_mat_joins:
            expected = sa.select([joined_sqla])
            self._compare_sqla(ibis_joined, expected)

        subselect_joins = [
            (
                region.inner_join(nation, ipred).projection(nation),
                rt.join(nt, spred),
            ),
            (
                region.left_join(nation, ipred).projection(nation),
                rt.join(nt, spred, isouter=True),
            ),
            (
                region.outer_join(nation, ipred).projection(nation),
                rt.outerjoin(nt, spred, full=True),
            ),
        ]
        for ibis_joined, joined_sqla in subselect_joins:
            expected = sa.select([nt]).select_from(joined_sqla)
            self._compare_sqla(ibis_joined, expected)

    def test_join_just_materialized(self):
        joined = self._case_join_just_materialized()

        rt, nt, ct = self._sqla_tables(
            ['tpch_region', 'tpch_nation', 'tpch_customer']
        )
        nt = nt.alias('t0')
        rt = rt.alias('t1')
        ct = ct.alias('t2')

        sqla_joined = nt.join(rt, nt.c.n_regionkey == rt.c.r_regionkey).join(
            ct, nt.c.n_nationkey == ct.c.c_nationkey
        )

        expected = sa.select([sqla_joined])

        self._compare_sqla(joined, expected)

    def test_full_outer_join(self):
        """Testing full outer join separately due to previous issue with
        outer join resulting in left outer join (issue #1773)"""
        region = self.con.table('tpch_region')
        nation = self.con.table('tpch_nation')

        predicate = region.r_regionkey == nation.n_regionkey
        joined = region.outer_join(nation, predicate)
        joined_sql_str = str(joined.compile())
        assert 'full' in joined_sql_str.lower()
        assert 'left' not in joined_sql_str.lower()

    def _sqla_tables(self, tables):
        result = []
        for t in tables:
            ibis_table = self.con.table(t)
            result.append(self._to_sqla(ibis_table))
        return result

    def test_simple_case(self):
        self.con.table('alltypes')
        st = self.con.meta.tables['alltypes']

        expr = self._case_simple_case()

        cases = [
            (
                expr,
                sa.case(
                    [
                        (st.c.g == L('foo'), L('bar')),
                        (st.c.g == L('baz'), L('qux')),
                    ],
                    else_='default',
                ),
            )
        ]
        self._check_expr_cases(cases)

    def test_searched_case(self):
        self.con.table('alltypes')
        st = self.con.meta.tables['alltypes']

        expr = self._case_search_case()
        cases = [
            (
                expr,
                sa.case(
                    [
                        (st.c.f > L(0), st.c.d * L(2)),
                        (st.c.c < L(0), st.c.a * L(2)),
                    ],
                    else_=sa.cast(sa.null(), sa.BIGINT),
                ),
            )
        ]
        self._check_expr_cases(cases)

    def test_where_simple_comparisons(self):
        expr = self._case_where_simple_comparisons()

        st = self.sa_star1.alias('t0')

        clause = sql.and_(st.c.f > L(0), st.c.c < (st.c.f * L(2)))
        expected = sa.select([st]).where(clause)

        self._compare_sqla(expr, expected)

    def test_simple_aggregate_query(self):
        st = self.sa_star1.alias('t0')

        cases = self._case_simple_aggregate_query()

        metric = F.sum(st.c.f).label('total')
        k1 = st.c.foo_id
        k2 = st.c.bar_id
        expected = [
            sa.select([k1, metric]).group_by(k1),
            sa.select([k1, k2, metric]).group_by(k1, k2),
        ]

        for case, ex_sqla in zip(cases, expected):
            self._compare_sqla(case, ex_sqla)

    def test_aggregate_having(self):
        st = self.sa_star1.alias('t0')

        cases = self._case_aggregate_having()

        metric = F.sum(st.c.f)
        k1 = st.c.foo_id
        expected = [
            sa.select([k1, metric.label('total')])
            .group_by(k1)
            .having(metric > L(10)),
            sa.select([k1, metric.label('total')])
            .group_by(k1)
            .having(F.count('*') > L(100)),
        ]

        for case, ex_sqla in zip(cases, expected):
            self._compare_sqla(case, ex_sqla)

    def test_sort_by(self):
        st = self.sa_star1.alias('t0')
        cases = self._case_sort_by()

        base = sa.select([st])
        expected = [
            base.order_by(st.c.f),
            base.order_by(st.c.f.desc()),
            base.order_by(st.c.c, st.c.f.desc()),
        ]
        for case, ex_sqla in zip(cases, expected):
            self._compare_sqla(case, ex_sqla)

    def test_limit(self):
        cases = self._case_limit()

        st = self.sa_star1.alias('t0')
        base = sa.select([st])

        expected = [
            base.limit(10),
            base.limit(10).offset(5),
            base.where(st.c.f > L(0)).limit(10),
        ]

        st = self.sa_star1.alias('t1')
        base = sa.select([st])
        aliased = base.limit(10).alias('t0')
        case4 = sa.select([aliased]).where(aliased.c.f > L(0))
        expected.append(case4)

        for case, ex in zip(cases, expected):
            self._compare_sqla(case, ex)

    def test_cte_factor_distinct_but_equal(self):
        expr = self._case_cte_factor_distinct_but_equal()

        alltypes = self._get_sqla('alltypes')

        t2 = alltypes.alias('t2')
        t0 = (
            sa.select([t2.c.g, F.sum(t2.c.f).label('metric')])
            .group_by(t2.c.g)
            .cte('t0')
        )

        t1 = t0.alias('t1')
        table_set = t0.join(t1, t0.c.g == t1.c.g)
        stmt = sa.select([t0]).select_from(table_set)

        self._compare_sqla(expr, stmt)

    def test_self_reference_join(self):
        t0 = self.sa_star1.alias('t0')
        t1 = self.sa_star1.alias('t1')

        case = self._case_self_reference_join()

        table_set = t0.join(t1, t0.c.foo_id == t1.c.bar_id)
        expected = sa.select([t0]).select_from(table_set)
        self._compare_sqla(case, expected)

    def test_self_reference_in_not_exists(self):
        semi, anti = self._case_self_reference_in_exists()

        s1 = self.sa_alltypes.alias('t0')
        s2 = self.sa_alltypes.alias('t1')

        cond = (
            sa.exists([L(1)])
            .select_from(s1)
            .where(s1.c.string_col == s2.c.string_col)
        )

        ex_semi = sa.select([s1]).where(cond)
        ex_anti = sa.select([s1]).where(~cond)

        self._compare_sqla(semi, ex_semi)
        self._compare_sqla(anti, ex_anti)

    def test_where_uncorrelated_subquery(self):
        expr = self._case_where_uncorrelated_subquery()

        foo = self._to_sqla(self.foo).alias('t0')
        bar = self._to_sqla(self.bar)

        subq = sa.select([bar.c.job])
        stmt = sa.select([foo]).where(foo.c.job.in_(subq))
        self._compare_sqla(expr, stmt)

    def test_where_correlated_subquery(self):
        expr = self._case_where_correlated_subquery()

        foo = self._to_sqla(self.foo)
        t0 = foo.alias('t0')
        t1 = foo.alias('t1')
        subq = sa.select([F.avg(t1.c.y).label('mean')]).where(
            t0.c.dept_id == t1.c.dept_id
        )
        # For versions of SQLAlchemy where scalar_subquery exists,
        # it should be used (otherwise, a deprecation warning is raised)
        if hasattr(subq, 'scalar_subquery'):
            subq = subq.scalar_subquery()
        stmt = sa.select([t0]).where(t0.c.y > subq)
        self._compare_sqla(expr, stmt)

    def test_subquery_aliased(self):
        expr = self._case_subquery_aliased()

        s1 = self._get_sqla('star1').alias('t2')
        s2 = self._get_sqla('star2').alias('t1')

        agged = (
            sa.select([s1.c.foo_id, F.sum(s1.c.f).label('total')])
            .group_by(s1.c.foo_id)
            .alias('t0')
        )

        joined = agged.join(s2, agged.c.foo_id == s2.c.foo_id)
        expected = sa.select([agged, s2.c.value1]).select_from(joined)

        self._compare_sqla(expr, expected)

    def test_lower_projection_sort_key(self):
        expr = self._case_subquery_aliased()

        t3 = self._get_sqla('star1').alias('t3')
        t2 = self._get_sqla('star2').alias('t2')
        t4 = (
            sa.select([t3.c.foo_id, F.sum(t3.c.f).label('total')])
            .group_by(t3.c.foo_id)
            .alias('t4')
        )
        t1 = (
            sa.select([t4.c.foo_id, t4.c.total, t2.c.value1])
            .select_from(t4.join(t2, t4.c.foo_id == t2.c.foo_id))
            .alias('t1')
        )
        t0 = (
            sa.select([t1.c.foo_id, t1.c.total, t1.c.value1])
            .where(t1.c.total > L(100))
            .alias('t0')
        )
        expected = sa.select([t0.c.foo_id, t0.c.total, t0.c.value1]).order_by(
            t0.c.total.desc()
        )

        expr2 = expr[expr.total > 100].sort_by(ibis.desc('total'))
        self._compare_sqla(expr2, expected)

    def test_exists(self):
        e1, e2 = self._case_exists()

        t1 = self._to_sqla(self.t1).alias('t0')
        t2 = self._to_sqla(self.t2).alias('t1')

        cond1 = sa.exists([L(1)]).where(t1.c.key1 == t2.c.key1)
        ex1 = sa.select([t1]).where(cond1)

        cond2 = sa.exists([L(1)]).where(
            sql.and_(t1.c.key1 == t2.c.key1, t2.c.key2 == L('foo'))
        )
        ex2 = sa.select([t1]).where(cond2)

        # pytest.skip('not yet implemented')

        self._compare_sqla(e1, ex1)
        self._compare_sqla(e2, ex2)

    def test_not_exists(self):
        expr = self._case_not_exists()

        t1 = self._to_sqla(self.t1).alias('t0')
        t2 = self._to_sqla(self.t2).alias('t1')

        cond1 = sa.exists([L(1)]).where(t1.c.key1 == t2.c.key1)
        expected = sa.select([t1]).where(sa.not_(cond1))

        self._compare_sqla(expr, expected)

    @pytest.mark.xfail(raises=AssertionError, reason='NYT')
    def test_general_sql_function(self):
        assert False

    @pytest.mark.xfail(raises=AssertionError, reason='NYT')
    def test_union(self):
        assert False

    def test_table_distinct(self):
        t = self.alltypes
        sat = self.sa_alltypes.alias('t0')

        cases = [
            (t.distinct(), sa.select([sat]).distinct()),
            (
                t['string_col', 'int_col'].distinct(),
                sa.select([sat.c.string_col, sat.c.int_col]).distinct(),
            ),
        ]
        for case, ex in cases:
            self._compare_sqla(case, ex)

    def test_array_distinct(self):
        t = self.alltypes
        sat = self.sa_alltypes.alias('t0')

        cases = [
            (t.string_col.distinct(), sa.select([sat.c.string_col.distinct()]))
        ]
        for case, ex in cases:
            self._compare_sqla(case, ex)

    def test_count_distinct(self):
        t = self.alltypes
        sat = self.sa_alltypes.alias('t0')

        cases = [
            (
                t.int_col.nunique().name('nunique'),
                sa.select(
                    [F.count(sat.c.int_col.distinct()).label('nunique')]
                ),
            ),
            (
                t.group_by('string_col').aggregate(
                    t.int_col.nunique().name('nunique')
                ),
                sa.select(
                    [
                        sat.c.string_col,
                        F.count(sat.c.int_col.distinct()).label('nunique'),
                    ]
                ).group_by(sat.c.string_col),
            ),
        ]
        for case, ex in cases:
            self._compare_sqla(case, ex)

    def test_sort_aggregation_translation_failure(self):
        # This works around a nuance with our choice to hackishly fuse SortBy
        # after Aggregate to produce a single select statement rather than an
        # inline view.
        t = self.alltypes

        agg = t.group_by('string_col').aggregate(
            t.double_col.max().name('foo')
        )
        expr = agg.sort_by(ibis.desc('foo'))

        sat = self.sa_alltypes.alias('t1')
        base = (
            sa.select(
                [sat.c.string_col, F.max(sat.c.double_col).label('foo')]
            ).group_by(sat.c.string_col)
        ).alias('t0')

        ex = (
            sa.select([base.c.string_col, base.c.foo])
            .select_from(base)
            .order_by(sa.desc('foo'))
        )

        self._compare_sqla(expr, ex)

    def test_where_correlated_subquery_with_join(self):
        # GH3163
        # ibis code
        part = ibis.table([("p_partkey", "int64")], name="part")
        partsupp = ibis.table(
            [
                ("ps_partkey", "int64"),
                ("ps_supplycost", "float64"),
                ("ps_suppkey", "int64"),
            ],
            name="partsupp",
        )
        supplier = ibis.table([("s_suppkey", "int64")], name="supplier")

        q = part.join(partsupp, part.p_partkey == partsupp.ps_partkey)
        q = q[
            part.p_partkey,
            partsupp.ps_supplycost,
        ]
        subq = partsupp.join(
            supplier, supplier.s_suppkey == partsupp.ps_suppkey
        )
        subq = subq.projection([partsupp.ps_partkey, partsupp.ps_supplycost])
        subq = subq[subq.ps_partkey == q.p_partkey]

        expr = q[q.ps_supplycost == subq.ps_supplycost.min()]

        # sqlalchemy code
        part = sa.table("part", sa.column("p_partkey"))
        supplier = sa.table("supplier", sa.column("s_suppkey"))
        partsupp = sa.table(
            "partsupp",
            sa.column("ps_partkey"),
            sa.column("ps_supplycost"),
            sa.column("ps_suppkey"),
        )

        part_t1 = part.alias("t1")
        partsupp_t2 = partsupp.alias("t2")

        t0 = (
            sa.select([part_t1.c.p_partkey, partsupp_t2.c.ps_supplycost])
            .select_from(
                part_t1.join(
                    partsupp_t2,
                    onclause=part_t1.c.p_partkey == partsupp_t2.c.ps_partkey,
                )
            )
            .alias("t0")
        )

        partsupp_t2 = partsupp.alias("t2")
        supplier_t5 = supplier.alias("t5")
        t3 = (
            sa.select([partsupp_t2.c.ps_partkey, partsupp_t2.c.ps_supplycost])
            .select_from(
                partsupp_t2.join(
                    supplier_t5,
                    onclause=supplier_t5.c.s_suppkey
                    == partsupp_t2.c.ps_suppkey,
                )
            )
            .alias("t3")
        )

        ex = (
            sa.select([t0.c.p_partkey, t0.c.ps_supplycost])
            .select_from(t0)
            .where(
                t0.c.ps_supplycost
                == (
                    sa.select([sa.func.min(t3.c.ps_supplycost).label("min")])
                    .select_from(t3)
                    .where(t3.c.ps_partkey == t0.c.p_partkey)
                    .as_scalar()
                )
            )
        )

        self._compare_sqla(expr, ex)

    def _compare_sqla(self, expr, sqla):
        context = AlchemyContext(compiler=AlchemyCompiler)
        result_sqla = AlchemyCompiler.to_sql(expr, context)
        result = str(result_sqla.compile())
        expected = str(sqla.compile())
        assert result == expected

    def _to_sqla(self, table):
        return table.op().sqla_table
