"""DeepSeek 图片批改回归测试；只替换 HTTP 传输，不调用真实 API。

运行: python tests/test_deepseek_vision.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from graders import agent, assignments, autodetect, extract, grading, providers


SPEC = assignments.TaskSpec(task_type="short", question="光合作用需要什么？",
                            reference="光照", points_text="光照 | 2", full_score=2)
REPLY = {"score": 2, "comment": "回答正确", "reason": "答到光照",
         "points": [{"point": "光照", "full": 2, "score": 2, "hit": "full", "evidence": "光照"}],
         "dimensions": [{"name": "内容", "score": 2, "full": 2}]}


def response(data):
    return Mock(json=lambda: {"choices": [{"message": {
        "role": "assistant", "content": json.dumps(data, ensure_ascii=False)}}]})


def has_images(messages):
    return any(isinstance(m.get("content"), list) and
               any(b.get("type") == "image_url" for b in m["content"]) for m in messages)


def tool_response(name, args):
    return Mock(json=lambda: {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": name, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}]}}]})


class DeepSeekVisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deepseek-vision-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        photo = root / "answer.png"
        Image.new("RGB", (80, 80), "white").save(photo)
        self.report = extract.inspect_image(photo, root, ocr_enabled=False)

    def choice(self, model="", use_vision=True):
        return providers.ModelChoice(provider="deepseek", api_key="test-key",
                                     model=model, use_vision=use_vision)

    def test_photo_assignment_sends_images_without_ocr(self):
        for model in ("", "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
            with self.subTest(model=model), patch.object(providers, "_post_with_retry",
                                                        return_value=response(REPLY)) as post:
                result = assignments.grade_submission(self.choice(model), SPEC, self.report)
                self.assertEqual(result["score"], 2)
                self.assertEqual(post.call_count, 1)
                url, _, payload = post.call_args.args
                self.assertEqual(url, "https://api.deepseek.com/chat/completions")
                self.assertEqual(payload["model"], model or "deepseek-flash")
                self.assertTrue(has_images(payload["messages"]))
                image_url = payload["messages"][-1]["content"][-1]["image_url"]["url"]
                self.assertTrue(image_url.startswith("data:image/jpeg;base64,"))
                self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_auto_segmentation_sends_images(self):
        segmented = {"doc_type": "exam", "questions": [{"no": "1", "type": "short",
                     "question": SPEC.question, "student_answer": "光照", "full_score": 2}]}
        with patch.object(providers, "_post_with_retry", return_value=response(segmented)) as post:
            result = autodetect.segment(self.choice(), self.report, [])
        self.assertEqual(result["questions"][0]["student_answer"], "光照")
        self.assertTrue(has_images(post.call_args.args[2]["messages"]))

    def test_photo_without_vision_or_ocr_is_rejected(self):
        for choice in (self.choice("deepseek-v4-pro"), self.choice("unknown-model"),
                       self.choice(use_vision=False)):
            with self.subTest(model=choice.model, vision=choice.use_vision), \
                    patch.object(providers, "_post_with_retry") as post:
                with self.assertRaisesRegex(providers.ProviderError, "视觉"):
                    assignments.grade_submission(choice, SPEC, self.report)
                with self.assertRaisesRegex(providers.ProviderError, "视觉"):
                    autodetect.segment(choice, self.report, [])
                post.assert_not_called()

    def test_pro_assignment_uses_available_ocr(self):
        self.report.full_text = "光照"
        with patch.object(providers, "_post_with_retry", return_value=response(REPLY)) as post:
            result = assignments.grade_submission(self.choice("deepseek-v4-pro"), SPEC, self.report)
        self.assertEqual(result["score"], 2)
        payload = post.call_args.args[2]
        self.assertFalse(has_images(payload["messages"]))
        self.assertIn("光照", payload["messages"][-1]["content"])
        self.assertEqual(payload["model"], "deepseek-v4-pro")

    def test_lab_grading_honors_model_capability(self):
        self.report.full_text = "光照"
        for model, expected in (("deepseek-flash", True), ("deepseek-v4-pro", False)):
            with self.subTest(model=model), patch.object(providers, "_post_with_retry",
                                                        return_value=response(REPLY)) as post:
                grading.grade_report(self.choice(model), "内容2分", self.report)
                self.assertEqual(has_images(post.call_args.args[2]["messages"]), expected)

    def test_agent_sends_images_only_to_vision_models(self):
        self.report.full_text = self.report.pages[0].ocr_text = "光照"
        for model, expected in (("deepseek-flash", True), ("deepseek-v4-pro", False)):
            replies = [tool_response("view_pages", {"page_nums": [1]}),
                       tool_response("submit_grade", REPLY)]
            with self.subTest(model=model), patch.object(providers, "_post_with_retry",
                                                        side_effect=replies) as post:
                result = agent.grade_report_agent(self.choice(model), "内容2分", self.report, max_steps=2)
                self.assertEqual(result["score"], 2)
                payload = post.call_args.args[2]
                self.assertEqual(has_images(payload["messages"]), expected)
                self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_other_providers_keep_image_support(self):
        for provider in ("openai", "custom"):
            choice = providers.ModelChoice(provider=provider, model="vision-model", api_key="test-key",
                                           base_url="https://example.invalid/v1")
            with self.subTest(provider=provider), patch.object(providers, "_post_with_retry",
                                                              return_value=response(REPLY)) as post:
                assignments.grade_submission(choice, SPEC, self.report)
                payload = post.call_args.args[2]
                self.assertTrue(has_images(payload["messages"]))
                self.assertNotIn("thinking", payload)


if __name__ == "__main__":
    unittest.main()
