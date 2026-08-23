"""CodexReader must survive the Codex log-format change (0.144 -> 0.149).

Old Codex logged an agent reply twice (event_msg/agent_message + response_item/message).
New Codex logs only the response_item form. Reading only the old form means the bridge
goes SILENT — typing indicator on, reply never delivered.
"""
import unittest

from agent2telegram.readers import CodexReader


def _texts(reader, records):
    out = []
    for rec in records:
        for ev in reader.parse(rec):
            if ev.kind == "text":
                out.append(ev.text)
    return out


OLD = [
    {"type": "event_msg", "timestamp": "t1",
     "payload": {"type": "agent_message", "message": "Ahoj Petře."}},
    {"type": "response_item", "timestamp": "t1",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Ahoj Petře."}]}},
]

NEW = [
    {"type": "response_item", "timestamp": "t1",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Ahoj Petře."}]}},
]


class CodexReaderFormatTests(unittest.TestCase):
    def test_old_format_is_not_double_sent(self):
        self.assertEqual(_texts(CodexReader(), OLD), ["Ahoj Petře."])

    def test_new_format_still_delivers(self):
        self.assertEqual(_texts(CodexReader(), NEW), ["Ahoj Petře."])

    def test_user_and_developer_messages_are_not_treated_as_replies(self):
        recs = [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "output_text", "text": "dotaz"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                  "content": [{"type": "output_text", "text": "systém"}]}},
        ]
        self.assertEqual(_texts(CodexReader(), recs), [])

    def test_multipart_content_is_joined(self):
        recs = [{"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "část 1 "},
                        {"type": "output_text", "text": "část 2"}]}}]
        self.assertEqual(_texts(CodexReader(), recs), ["část 1 část 2"])

    def test_empty_or_malformed_payload_is_ignored(self):
        recs = [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "nope"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "   "}]}},
        ]
        self.assertEqual(_texts(CodexReader(), recs), [])

    def test_tool_calls_still_parsed(self):
        rec = {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1",
                                                    "name": "shell", "arguments": "{}"}}
        kinds = [ev.kind for ev in CodexReader().parse(rec)]
        self.assertEqual(kinds, ["tool"])


if __name__ == "__main__":
    unittest.main()
