from slack_worker.demo import run


def test_loopback_http_burst_and_duplicates():
    result = run()
    assert result["enqueued"] == 200
    assert result["model_calls"] == result["slack_posts"] == 100
