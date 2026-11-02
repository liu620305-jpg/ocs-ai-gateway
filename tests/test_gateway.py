"""网关的单元测试与接口测试。

运行：uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.cache import AnswerCache  # noqa: E402
from app.llm import ProviderError  # noqa: E402
from app.parsing import normalize_type, parse_answer, parse_options  # noqa: E402
from app.store import ProviderStore  # noqa: E402


class ParsingTests(unittest.TestCase):
    def test_normalize_type(self):
        self.assertEqual(normalize_type("single"), "single")
        self.assertEqual(normalize_type("Multiple"), "multiple")
        self.assertEqual(normalize_type("judgement"), "judge")
        self.assertEqual(normalize_type("tf"), "judge")
        self.assertEqual(normalize_type(None), "single")
        self.assertIsNone(normalize_type("completion"))

    def test_parse_options(self):
        self.assertEqual(parse_options('["北京","上海"]'), ["北京", "上海"])
        self.assertEqual(parse_options(["A", "B"]), ["A", "B"])
        self.assertEqual(parse_options("A. 北京\nB. 上海"), ["北京", "上海"])
        self.assertEqual(parse_options(""), [])

    def test_single(self):
        options = ["北京", "上海", "广州"]
        self.assertEqual(parse_answer("single", '{"answer": "B"}', options), "B")
        self.assertEqual(parse_answer("single", "答案：C。", options), "C")
        self.assertEqual(parse_answer("single", "B. 上海", options), "B")
        self.assertEqual(parse_answer("single", "上海", options), "B")
        self.assertIsNone(parse_answer("single", "不知道", options))
        self.assertIsNone(parse_answer("single", '{"answer": "Z"}', options))

    def test_multiple(self):
        options = ["甲", "乙", "丙", "丁"]
        self.assertEqual(parse_answer("multiple", '{"answer":"A#C"}', options), "A#C")
        self.assertEqual(parse_answer("multiple", "A、C、D", options), "A#C#D")
        self.assertEqual(parse_answer("multiple", "C,A", options), "A#C")
        self.assertIsNone(parse_answer("multiple", "都不对", options))

    def test_judge(self):
        self.assertEqual(parse_answer("judge", '{"answer":"正确"}', []), "正确")
        self.assertEqual(parse_answer("judge", "错误", []), "错误")
        self.assertEqual(parse_answer("judge", "不正确", []), "错误")
        self.assertEqual(parse_answer("judge", "√", []), "正确")
        self.assertEqual(parse_answer("judge", "A", ["正确", "错误"]), "正确")
        self.assertIsNone(parse_answer("judge", "可能吧", []))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        main.store = ProviderStore(os.path.join(self.tmp.name, "providers.json"))
        main.cache = AnswerCache(os.path.join(self.tmp.name, "answers.db"), 168)
        main.stats = main.Stats()
        main.store.add(
            {
                "name": "主 AI",
                "base_url": "https://example.com/v1",
                "api_key": "sk-test-1234",
                "model": "test-model",
            }
        )
        self.client = TestClient(main.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_health(self):
        data = self.client.get("/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["providers"], 1)

    def test_query_success_and_cache(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"B"}')):
            first = self.client.get(
                "/query", params={"title": "首都是哪", "type": "single", "options": '["北京","上海"]'}
            ).json()
            second = self.client.get(
                "/query", params={"title": "首都是哪", "type": "single", "options": '["北京","上海"]'}
            ).json()
        self.assertEqual(first["code"], 0)
        self.assertEqual(first["data"]["answer"], "B")
        self.assertFalse(first["data"]["cached"])
        self.assertTrue(second["data"]["cached"])
        self.assertEqual(main.stats.total, 2)
        self.assertEqual(main.stats.cached, 1)

    def test_query_post(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value="A#C")):
            data = self.client.post(
                "/query",
                json={"title": "多选", "type": "multiple", "options": ["甲", "乙", "丙"]},
            ).json()
        self.assertEqual(data["data"]["answer"], "A#C")

    def test_failover_to_second_provider(self):
        main.store.add(
            {
                "name": "备用 AI",
                "base_url": "https://backup.example.com/v1",
                "api_key": "sk-backup",
                "model": "backup-model",
            }
        )
        calls = []

        async def fake(provider, messages):
            calls.append(provider["name"])
            if provider["name"] == "主 AI":
                raise ProviderError("HTTP 401: invalid key")
            return '{"answer":"A"}'

        with mock.patch.object(main, "call_provider", fake):
            data = self.client.get(
                "/query", params={"title": "题", "type": "single", "options": '["x","y"]'}
            ).json()

        self.assertEqual(data["code"], 0)
        self.assertEqual(data["data"]["provider"], "备用 AI")
        self.assertEqual(calls, ["主 AI", "备用 AI"])
        self.assertEqual(main.stats.failovers, 1)

    def test_all_providers_fail(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(side_effect=ProviderError("boom"))):
            data = self.client.get(
                "/query", params={"title": "题", "type": "single", "options": '["x","y"]'}
            ).json()
        self.assertEqual(data["code"], 50001)
        self.assertIn("boom", data["message"])

    def test_unsupported_type(self):
        data = self.client.get("/query", params={"title": "填空", "type": "completion"}).json()
        self.assertEqual(data["code"], 40002)

    def test_missing_title(self):
        data = self.client.get("/query", params={"type": "single"}).json()
        self.assertEqual(data["code"], 40001)

    def test_admin_crud(self):
        created = self.client.post(
            "/api/providers",
            json={
                "name": "新 AI",
                "base_url": "https://new.example.com/v1",
                "api_key": "sk-new-9999",
                "model": "new-model",
            },
        )
        self.assertEqual(created.status_code, 200)
        pid = created.json()["id"]
        self.assertNotIn("sk-new-9999", created.text)

        state = self.client.get("/api/state").json()
        self.assertEqual(len(state["providers"]), 2)

        updated = self.client.put(f"/api/providers/{pid}", json={"name": "改名了", "base_url": "https://new.example.com/v1", "model": "new-model"})
        self.assertEqual(updated.json()["name"], "改名了")
        self.assertEqual(updated.json()["has_key"], True)

        self.assertTrue(self.client.delete(f"/api/providers/{pid}").json()["ok"])
        self.assertEqual(len(self.client.get("/api/state").json()["providers"]), 1)

    def test_provider_test_endpoint(self):
        pid = self.client.get("/api/state").json()["providers"][0]["id"]
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            data = self.client.post(f"/api/providers/{pid}/test").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["answer"], "A")

    def test_cache_clear(self):
        with mock.patch.object(main, "call_provider", mock.AsyncMock(return_value='{"answer":"A"}')):
            self.client.get("/query", params={"title": "t", "type": "single", "options": '["x","y"]'})
        self.assertEqual(self.client.post("/api/cache/clear").json()["cleared"], 1)
        self.assertEqual(self.client.get("/health").json()["cache"], 0)


if __name__ == "__main__":
    unittest.main()
