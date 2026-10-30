"""Declarative WorkBuddy conversion cases.

tests/workbuddy_cases.json holds the fixtures as data — input and expected
output — in the pattern of magpie-community/plugins' cases.json: each case is
driven through workbuddy_protocol exactly the way gateway_server calls it, so
what a case means reads from the file, not from test code.
"""
import json
from pathlib import Path
import unittest

from workbuddy_protocol import after_tools_separator, schema, to_chat

CASES = json.loads((Path(__file__).with_name('workbuddy_cases.json')).read_text(encoding='utf-8'))


class SchemaCases(unittest.TestCase):
    def test_schema_cases(self):
        for case in CASES['schema']:
            with self.subTest(case['name']):
                self.assertEqual(schema(case['input']), case['expect'])


class AfterToolsCases(unittest.TestCase):
    def test_after_tools_cases(self):
        for case in CASES['after_tools']:
            with self.subTest(case['name']):
                messages = [dict(m) for m in case['messages']]
                inserted = after_tools_separator(messages)
                self.assertEqual(inserted, case['inserted'])
                self.assertEqual(messages, case['expect_messages'])


class ChatCases(unittest.TestCase):
    def test_chat_cases(self):
        for case in CASES['chat']:
            with self.subTest(case['name']):
                snapshot = json.dumps(case['request'], sort_keys=True)
                chat, names = to_chat(dict(case['request'], model=case.get('model', 'm')),
                                      case['protocol'], case.get('previous'))
                self.assertEqual(chat, case['expect_chat'])
                self.assertEqual(dict(names), case.get('expect_names', {}))
                # Conversion works on a copy; the client's request is not mutated.
                self.assertEqual(json.dumps(case['request'], sort_keys=True), snapshot)


class ErrorCases(unittest.TestCase):
    def test_error_cases(self):
        for case in CASES['errors']:
            with self.subTest(case['name']):
                with self.assertRaises(ValueError) as raised:
                    to_chat(dict(case['request'], model='m'), case['protocol'])
                self.assertIn(case['expect_error'], str(raised.exception))


if __name__ == '__main__':
    unittest.main()
