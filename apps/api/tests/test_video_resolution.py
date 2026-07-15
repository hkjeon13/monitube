import pytest

from monitube_api.video_resolution import VideoInputError, VideoInputKind, resolve_video_input


def test_normalizes_direct_id_watch_url_and_short_url_without_network_access() -> None:
    direct = resolve_video_input("dQw4w9WgXcQ")
    watch = resolve_video_input("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share")
    short = resolve_video_input("youtu.be/dQw4w9WgXcQ?t=42")

    assert direct.kind is VideoInputKind.VIDEO_ID
    assert watch.kind is VideoInputKind.WATCH_URL
    assert short.kind is VideoInputKind.SHORT_URL
    assert {direct.normalized, watch.normalized, short.normalized} == {"dQw4w9WgXcQ"}


def test_rejects_external_urls_and_invalid_video_ids() -> None:
    with pytest.raises(VideoInputError):
        resolve_video_input("https://example.com/watch?v=dQw4w9WgXcQ")
    with pytest.raises(VideoInputError):
        resolve_video_input("not-a-video-id")
