import pytest

from datawrench.contract_diff.types import TypeParseError, compare_types, parse_type


@pytest.mark.parametrize('dialect,a,b,expected,breaking', [
    ('duckdb', 'int', 'integer', 'identical', False),
    ('duckdb', 'int', 'bigint', 'widening', False),
    ('duckdb', 'bigint', 'int', 'narrowing', True),
    ('duckdb', 'smallint', 'int', 'widening', False),
    ('duckdb', 'tinyint', 'smallint', 'widening', False),
    ('duckdb', 'int', 'varchar', 'incompatible', True),
    ('duckdb', 'string', 'varchar', 'identical', False),
    ('duckdb', 'varchar', 'text', 'identical', False),
    ('duckdb', 'float', 'double', 'identical', False),
    ('duckdb', 'float64', 'double', 'identical', False),
    ('duckdb', 'varchar(10)', 'varchar(20)', 'widening', False),
    ('duckdb', 'varchar(20)', 'varchar(10)', 'narrowing', True),
    ('duckdb', 'varchar(20)', 'text', 'widening', False),
    ('duckdb', 'text', 'varchar(20)', 'narrowing', True),
    ('duckdb', 'decimal(10,2)', 'decimal(12,2)', 'widening', False),
    ('duckdb', 'decimal(10,2)', 'decimal(12,4)', 'widening', False),
    ('duckdb', 'decimal(10,2)', 'decimal(10,4)', 'incompatible', True),
    ('duckdb', 'decimal(12,4)', 'decimal(10,2)', 'narrowing', True),
    ('duckdb', 'decimal(10)', 'decimal(10,0)', 'identical', False),
    ('duckdb', 'timestamp', 'timestamp with time zone', 'incompatible', True),
    ('postgres', 'int4', 'integer', 'identical', False),
    ('postgres', 'integer', 'bigint', 'widening', False),
    ('postgres', 'character varying(10)', 'varchar(20)', 'widening', False),
    ('postgres', 'numeric(10,2)', 'decimal(12,2)', 'widening', False),
    ('postgres', 'float', 'double precision', 'identical', False),
    ('postgres', 'boolean', 'integer', 'incompatible', True),
    ('bigquery', 'int64', 'bigint', 'identical', False),
    ('bigquery', 'float64', 'double', 'identical', False),
    ('bigquery', 'string', 'varchar', 'identical', False),
    ('bigquery', 'numeric(10,2)', 'numeric(9,2)', 'narrowing', True),
    ('snowflake', 'string', 'varchar', 'identical', False),
    ('snowflake', 'number(10,2)', 'number(12,2)', 'widening', False),
    ('snowflake', 'float', 'double', 'identical', False),
    ('snowflake', 'varchar(10)', 'varchar(5)', 'narrowing', True),
    ('clickhouse', 'Int32', 'Int64', 'widening', False),
    ('clickhouse', 'UInt32', 'Int64', 'widening', False),
    ('clickhouse', 'Int32', 'UInt32', 'incompatible', True),
    ('clickhouse', 'UInt64', 'UInt32', 'narrowing', True),
    ('clickhouse', 'Float64', 'Float32', 'identical', False),
    ('clickhouse', 'LowCardinality(String)', 'String', 'identical', False),
    ('clickhouse', 'LowCardinality(Nullable(String))', 'Nullable(String)', 'identical', False),
    ('databricks', 'string', 'varchar', 'identical', False),
    ('databricks', 'int', 'bigint', 'widening', False),
    ('redshift', 'decimal(10,2)', 'decimal(12,2)', 'widening', False),
    ('redshift', 'varchar(10)', 'varchar(20)', 'widening', False),
])
def test_classification(dialect, a, b, expected, breaking):
    result = compare_types(a, b, dialect)
    assert not result.warnings
    assert result.classification == expected
    assert result.breaking is breaking


@pytest.mark.parametrize('a,b,dialect,path,breaking', [
    ('struct<a int>', 'struct<a int,b string>', 'duckdb', 'type.b', False),
    ('struct<a int,b string>', 'struct<a int>', 'duckdb', 'type.b', True),
    ('struct<a int>', 'struct<a bigint>', 'duckdb', 'type.a', True),
    ('struct<a decimal(10,2)>', 'struct<a varchar>', 'duckdb', 'type.a', True),
    ('struct<a int,b int>', 'struct<b int,a int>', 'duckdb', 'type', True),
    ('array<int>', 'array<bigint>', 'bigquery', 'type.element', True),
    ('array<struct<a int>>', 'array<struct<a string>>', 'bigquery', 'type.element.a', True),
    ('map<string,int>', 'map<int,int>', 'databricks', 'type.key', True),
    ('map<string,int>', 'map<string,bigint>', 'databricks', 'type.value', True),
    ('map<string,struct<a int>>', 'map<string,struct<a int,b int>>', 'databricks', 'type.value.b', False),
    ('Nullable(Int32)', 'Int32', 'clickhouse', 'type.nullable', True),
    ('Int32', 'Nullable(Int32)', 'clickhouse', 'type.nullable', False),
    ('LowCardinality(Nullable(Int32))', 'LowCardinality(Nullable(String))', 'clickhouse', 'type', True),
    ('Tuple(a Int32, b String)', 'Tuple(a Int32)', 'clickhouse', 'type.b', True),
    ('int[]', 'bigint[]', 'postgres', 'type.element', True),
])
def test_nested(a, b, dialect, path, breaking):
    result = compare_types(a, b, dialect)
    assert not result.warnings
    assert result.details[0].path == path
    assert result.breaking is breaking


@pytest.mark.parametrize('a,b', [('int', 'bigint'), ('varchar(10)', 'varchar(20)'), ('decimal(10,2)', 'decimal(12,2)'), ('Int32', 'Nullable(Int32)')])
def test_strict_widening(a, b):
    assert not compare_types(a, b, 'clickhouse').breaking
    assert compare_types(a, b, 'clickhouse', strict_widening=True).breaking


@pytest.mark.parametrize('value', ['no_such_type', 'int garbage', 'int; DROP TABLE secrets', 'int -- comment',
                                   'array<>', 'decimal(2,9)', 'varchar(-1)', 'decimal(1+1,0)', 'int[3]',
                                   'struct<a int,a int>', 'Tuple(Int32, String)', '', 'int /* comment */',
                                   'array<' * 70 + 'int' + '>' * 70, 'x' * 8193])
def test_reject_unsupported_or_malformed(value):
    with pytest.raises(TypeParseError):
        parse_type(value, 'clickhouse')


def test_opaque_fallback_explicit():
    equal = compare_types('  custom_type ', 'custom_type', 'postgres')
    assert equal.classification == 'unparseable' and not equal.breaking and len(equal.warnings) == 2
    changed = compare_types('custom_type', 'CUSTOM_TYPE', 'postgres')
    assert changed.breaking and changed.warnings
    assert compare_types(None, None).breaking
    assert compare_types('Mood(\'A B\')', 'Mood(\'A  B\')').breaking


def test_quoted_fields_not_folded():
    result = compare_types('STRUCT("A" INT)', 'STRUCT("a" INT)')
    assert result.breaking
    assert {d.classification for d in result.details} == {'field_added', 'field_removed'}


def test_nullable_and_type_change_both_retained():
    result = compare_types('Nullable(Int32)', 'Int64', 'clickhouse')
    assert result.breaking
    assert [d.classification for d in result.details] == ['narrowing', 'widening']


@pytest.mark.parametrize('dialect', ['duckdb', 'postgres', 'snowflake', 'bigquery', 'databricks', 'redshift', 'clickhouse'])
def test_nested_float_alias_and_field_name_preserved(dialect):
    result = compare_types('STRUCT<a FLOAT64, float64 INT>', 'STRUCT<a DOUBLE, float64 INT>', dialect)
    assert result.classification == 'identical' and not result.warnings


def test_quoted_and_unquoted_field_names_remain_distinct():
    assert compare_types('STRUCT("A" INT)', 'STRUCT(A INT)', 'postgres').breaking
    assert compare_types('STRUCT("a" INT)', 'STRUCT("a" INTEGER)', 'postgres').classification == 'identical'


def test_parameter_units_never_silently_equal():
    result = compare_types('varchar(10 BYTE)', 'varchar(10 CHAR)', 'snowflake')
    assert result.classification == 'unparseable' and result.breaking and result.warnings
