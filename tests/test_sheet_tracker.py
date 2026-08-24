import sheet_tracker


def test_only_real_responses_count_as_heard_back():
    assert sheet_tracker._is_response_event("offer") is True
    assert sheet_tracker._is_response_event("interview_invite") is True
    assert sheet_tracker._is_response_event("oa_invite") is True
    assert sheet_tracker._is_response_event("recruiter_reply") is True
    assert sheet_tracker._is_response_event("rejection") is True
    assert sheet_tracker._is_response_event("confirmation") is False
    assert sheet_tracker._is_response_event("application_received") is False
