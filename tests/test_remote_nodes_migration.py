"""r0039: the `ssh` transport becomes `remote`, and `ssh` is no longer taken."""
import sqlite3
import unittest

from db.migration_revisions import r0039_remote_nodes


class RemoteNodesMigrationTests(unittest.TestCase):
    def test_ssh_nodes_become_remote(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE backend_nodes (key TEXT PRIMARY KEY)')
        conn.execute('''CREATE TABLE backend_node_connections (
            node_key TEXT PRIMARY KEY REFERENCES backend_nodes(key) ON DELETE CASCADE,
            transport TEXT NOT NULL CHECK(transport IN ('local', 'ssh')), ssh_target TEXT)''')
        conn.executemany('INSERT INTO backend_nodes VALUES (?)', [('a',), ('b',), ('c',)])
        conn.executemany('INSERT INTO backend_node_connections VALUES (?,?,?)',
                         [('a', 'local', None), ('b', 'ssh', 'root@b')])
        r0039_remote_nodes.upgrade(conn)
        self.assertEqual(conn.execute('SELECT * FROM backend_node_connections ORDER BY node_key').fetchall(),
                         [('a', 'local', None), ('b', 'remote', 'root@b')])
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO backend_node_connections VALUES ('c', 'ssh', 'root@c')")
