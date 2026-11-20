import asyncio
import io
import json
from pathlib import Path
import sys
import unittest

from collector.claude_collector import Collector, MAX_LINE_BYTES, supervise


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.output.name = 'test.jsonl'
        self.collector = Collector(self.output)

    def events(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def feed(self, **event):
        self.collector.consume(json.dumps(event).encode())

    def test_normalize_without_tool_payloads(self):
        self.collector.consume(b'broken JSON')
        self.feed(type='future_event', secret='not stored')
        self.feed(type='assistant', message={'content': [
            {'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'path': 'private'}},
        ]})
        self.feed(type='user', message={'content': [
            {'type': 'tool_result', 'tool_use_id': 'read-1', 'content': 'private'},
        ]})
        events = self.events()
        self.assertEqual([row['seq'] for row in events], list(range(1, 6)))
        self.assertEqual(events[2]['tool_use_id'], events[4]['tool_use_id'])
        self.assertEqual(self.collector.invalid_lines, 1)
        self.assertNotIn('private', self.output.getvalue())
        self.assertNotIn('not stored', self.output.getvalue())

    def test_zero_exit_without_result_fails(self):
        self.assertEqual(self.collector.finish(0, 1)['error'], 'missing_result')
        self.feed(type='result', subtype='success', is_error=True)
        self.assertEqual(self.collector.finish(0, 1)['error'], 'unsuccessful_result')

    async def test_subprocess_drains_stderr_and_recovers_after_long_line(self):
        script = (
            'import sys\n'
            "sys.stderr.write('w' * 100000)\n"
            f"print('x' * {MAX_LINE_BYTES + 100})\n"
            "print('{\"type\":\"result\",\"subtype\":\"success\","
            "\"is_error\":false,\"result\":\"DEMO_OK\"}')\n"
        )
        summary = await supervise([sys.executable, '-I', '-c', script], Path.cwd(), 10, self.collector)
        self.assertEqual(summary['status'], 'succeeded')
        self.assertEqual(summary['stderr_bytes'], 100000)
        self.assertEqual(summary['oversize_line_count'], 1)
        self.assertEqual(summary['terminal']['result'], 'DEMO_OK')
        self.assertNotIn('DEMO_OK', self.output.getvalue())

    async def test_timeout_terminates_and_preserves_prior_events(self):
        script = 'import time; print(\'{"type":"system","subtype":"init"}\', flush=True); time.sleep(20)'
        summary = await supervise([sys.executable, '-I', '-c', script], Path.cwd(), 2, self.collector)
        self.assertEqual(summary['status'], 'timed_out')
        self.assertIsNotNone(summary['child_exit_code'])
        self.assertEqual(self.events()[0]['kind'], 'session_started')


if __name__ == '__main__':
    unittest.main()
