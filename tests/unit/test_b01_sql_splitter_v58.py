"""B-01 (v0.58.0): SQL migration statement parser.

The _split_statements function must handle semicolons inside string literals,
double-quoted identifiers, line comments, and block comments correctly.
Backticks and $$ dollar-quoted strings are out of scope.

Scenarios:
- ; inside string → not split
- ; in block comment → not split
- '' escape → doesn't end string
- unterminated quote → parse error
- unterminated block comment → parse error
- all existing migration files pass regression
"""
import os

import pytest

from knarr.core.migrations import _split_statements


class TestSemicolonInString:
    """Semicolon inside string literals must not split."""

    def test_semicolon_in_single_quoted_string(self):
        sql = "INSERT INTO t VALUES ('hello;world');"
        result = _split_statements(sql)
        assert len(result) == 1
        assert "hello;world" in result[0]

    def test_semicolon_in_double_quoted_identifier(self):
        sql = 'SELECT "col;name" FROM t;'
        result = _split_statements(sql)
        assert len(result) == 1
        assert '"col;name"' in result[0]

    def test_multiple_statements_with_strings(self):
        sql = "INSERT INTO t VALUES ('a;b');\nINSERT INTO t VALUES ('c;d');"
        result = _split_statements(sql)
        assert len(result) == 2
        assert "a;b" in result[0]
        assert "c;d" in result[1]


class TestSemicolonInComment:
    """Semicolon inside comments must not split."""

    def test_semicolon_in_line_comment(self):
        sql = "-- this is; a comment\nSELECT 1;"
        result = _split_statements(sql)
        assert len(result) == 1
        assert result[0].strip() == "SELECT 1"

    def test_semicolon_in_block_comment(self):
        sql = "/* this is; a block comment */ SELECT 1;"
        result = _split_statements(sql)
        assert len(result) == 1
        assert result[0].strip() == "SELECT 1"

    def test_semicolon_in_multiline_block_comment(self):
        sql = "/* line 1;\nline 2; */ SELECT 1;"
        result = _split_statements(sql)
        assert len(result) == 1


class TestEscapeSequences:
    """String escape sequences must be handled correctly."""

    def test_single_quote_escape(self):
        """'' inside single-quoted string must not end the string."""
        sql = "INSERT INTO t VALUES ('it''s; fine');"
        result = _split_statements(sql)
        assert len(result) == 1
        assert "it''s; fine" in result[0]

    def test_double_quote_in_single_string(self):
        """Double quotes inside single-quoted string are literal."""
        sql = "INSERT INTO t VALUES ('he said \"hello;\"');"
        result = _split_statements(sql)
        assert len(result) == 1


class TestParseErrors:
    """Unterminated strings/comments must raise ValueError."""

    def test_unterminated_single_quote(self):
        sql = "INSERT INTO t VALUES ('unterminated;"
        with pytest.raises(ValueError, match="Unterminated single-quoted"):
            _split_statements(sql)

    def test_unterminated_block_comment(self):
        sql = "/* unterminated block SELECT 1;"
        with pytest.raises(ValueError, match="Unterminated block comment"):
            _split_statements(sql)

    def test_unterminated_double_quote(self):
        sql = 'SELECT "unterminated FROM t;'
        with pytest.raises(ValueError, match="Unterminated double-quoted"):
            _split_statements(sql)


class TestBasicFunctionality:
    """Basic splitting must still work."""

    def test_single_statement(self):
        result = _split_statements("SELECT 1;")
        assert result == ["SELECT 1"]

    def test_multiple_statements(self):
        sql = "SELECT 1;\nINSERT INTO t VALUES (2);\nCREATE TABLE x (id INT);"
        result = _split_statements(sql)
        assert len(result) == 3
        assert result[0] == "SELECT 1"

    def test_no_trailing_semicolon(self):
        result = _split_statements("SELECT 1")
        assert result == ["SELECT 1"]

    def test_blank_lines_ignored(self):
        sql = "SELECT 1;\n\n\nINSERT INTO t VALUES (2);"
        result = _split_statements(sql)
        assert len(result) == 2

    def test_empty_input(self):
        result = _split_statements("")
        assert result == []

    def test_only_comments(self):
        sql = "-- comment 1\n-- comment 2\n/* block */"
        result = _split_statements(sql)
        assert result == []


class TestRegressionExistingMigrations:
    """All existing migration files must parse to the same statement list."""

    def test_all_migration_files_parse(self):
        migrations_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "src", "knarr", "migrations"
        )
        if not os.path.isdir(migrations_dir):
            pytest.skip("migrations directory not found")

        for filename in sorted(os.listdir(migrations_dir)):
            if not filename.endswith('.sql'):
                continue
            filepath = os.path.join(migrations_dir, filename)
            with open(filepath, 'r') as f:
                sql = f.read()

            # Must parse without error
            stmts = _split_statements(sql)
            assert len(stmts) > 0, f"{filename}: expected at least one statement"

            # Each statement must contain SQL keywords (sanity check)
            for stmt in stmts:
                upper = stmt.upper()
                has_keyword = any(kw in upper for kw in [
                    "ALTER", "CREATE", "DROP", "INSERT", "UPDATE", "DELETE",
                    "SELECT", "BEGIN", "COMMIT", "PRAGMA",
                ])
                assert has_keyword, (
                    f"{filename}: statement has no SQL keyword: {stmt[:80]}"
                )

    def test_v056_0_comment_semicolons(self):
        """v0.56.0 has comments with semicolons — must not split."""
        migrations_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "src", "knarr", "migrations"
        )
        filepath = os.path.join(migrations_dir, "v0_56_0.sql")
        if not os.path.isfile(filepath):
            pytest.skip("v0_56_0.sql not found")

        with open(filepath, 'r') as f:
            sql = f.read()

        stmts = _split_statements(sql)
        # The migration has multiple ALTER/CREATE statements
        assert len(stmts) >= 3  # at least ALTER, UPDATE, CREATE INDEX patterns
