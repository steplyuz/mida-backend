from app.core.exceptions import problem_response


def test_problem_response_has_problem_json_media_type() -> None:
    response = problem_response(404, type_="urn:mida:error:http-404", detail="Missing")
    assert response.status_code == 404
    assert response.media_type == "application/problem+json"


def test_httpx_logger_does_not_leak_bot_token_urls() -> None:
    import logging

    from app.core.logging import setup_logging

    setup_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
