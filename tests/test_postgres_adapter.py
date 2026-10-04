"""Exercise psycopg's real parameter parser rather than a SQL fixture substitute."""
import os
import subprocess
import sys
import unittest


class PostgresParameterTests(unittest.TestCase):
    def test_literal_percent_and_quoted_question_mark_survive_binding(self):
        program = '''
from db.postgres_db import _translate_query
from psycopg._queries import _query2pg
sql = "SELECT ? WHERE value LIKE ('%' || '?' || '%') AND progress = '100%'"
translated = _translate_query(sql, ('value',))
query, formats, names, parts = _query2pg(translated.encode(), 'utf-8')
assert query == b"SELECT $1 WHERE value LIKE ('%' || '?' || '%') AND progress = '100%'", query
assert len(formats) == 1
'''
        result = subprocess.run([sys.executable, '-c', program], env=os.environ.copy(),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

