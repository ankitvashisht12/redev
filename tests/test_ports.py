import socket
import tempfile
from pathlib import Path
import unittest
from redev.state import StateStore


class PortTests(unittest.TestCase):
    def test_distinct_worktrees_reserve_distinct_ports_and_explicit_conflict_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = StateStore(base / 'first', base / 'state')
            second = StateStore(base / 'second', base / 'state')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                preferred = listener.getsockname()[1]
            first_ports = first.allocate_ports({'web': preferred})
            second_ports = second.allocate_ports({'web': preferred})
            self.assertEqual(first_ports['web'], preferred)
            self.assertNotEqual(second_ports['web'], preferred)
            with self.assertRaisesRegex(RuntimeError, 'in use'):
                second.allocate_ports({'web': preferred}, overrides={'web': preferred})

    def test_saved_port_is_not_silently_changed_when_another_app_takes_it(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            occupied = listener.getsockname()[1]
            store = StateStore(Path(temporary) / 'repo', Path(temporary) / 'state')
            with self.assertRaisesRegex(RuntimeError, '--port web=NUMBER'):
                store.allocate_ports({'web': 8080}, previous={'web': occupied})

if __name__ == '__main__':
    unittest.main()
