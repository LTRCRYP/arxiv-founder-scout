import json
from types import SimpleNamespace

from scout import screen
from scout.ingest import parse_feed, upsert_paper


class FakeMessages:
    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        title = kwargs["messages"][0]["content"].split("\n", 1)[0]
        payload = self.payloads.get(title, self.payloads["default"])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def fake_client(payloads):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(payloads)))


def test_deterministic_and_llm_scores_compose(cfg, conn, feed_xml):
    for e in parse_feed(feed_xml)[1]:
        upsert_paper(conn, e, run_id=1)
    conn.execute("UPDATE authors SET h_index=25, citation_count=4000, enriched_at='x' WHERE normalized_name='jose garcia lopez'")
    high = {c: {"score": 9, "reason": "strong"} for c in screen.LLM_CRITERIA}
    low = {c: {"score": 2, "reason": "survey"} for c in screen.LLM_CRITERIA}
    client = fake_client({"Title: Robotic Manipulation with Foundation Models": high, "default": low})

    stats = screen.run(cfg, conn, client=client)
    assert stats["llm_scored"] == 3 and stats["llm_failed"] == 0
    assert client.beta.messages.calls[0]["output_config"]["format"]["type"] == "json_schema"

    def score(aid, crit):
        return conn.execute("SELECT score, reason FROM paper_scores WHERE arxiv_id=? AND criterion=?", (aid, crit)).fetchone()

    assert score("2611.00001", "code_released")[0] == 10
    assert score("2611.00002", "code_released")[0] == 0
    assert score("2611.00003", "code_released")[0] == 4  # mentions open-source release without a link
    assert score("2611.00001", "author_track_record")[0] > 8
    assert score("2611.00002", "author_track_record")[1].startswith("no citation data")
    assert score("2611.00001", "composite")[0] > score("2611.00002", "composite")[0]
    assert score("2611.00001", "topic_fit")[1] == "strong"

    # second run scores nothing new; --rescore re-sends
    assert screen.run(cfg, conn, client=client)["papers"] == 0
    assert screen.run(cfg, conn, client=client, rescore=True)["llm_scored"] == 3


def test_refusal_and_bad_json_are_handled():
    msg = SimpleNamespace(stop_reason="refusal", stop_details=None, content=[])
    assert screen.parse_llm_response(msg) is None
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='noise {"topic_fit":{"score":7,"reason":"ok"},"novelty":{"score":5,"reason":"meh"},"commercial_applicability":{"score":11,"reason":"x"}} trailing')])
    out = screen.parse_llm_response(msg)
    assert out["topic_fit"] == (7.0, "ok") and out["commercial_applicability"][0] == 10.0
