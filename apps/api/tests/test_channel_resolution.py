import pytest

from monitube_api.channel_resolution import ChannelInputError, ChannelInputKind, resolve_channel_input


def test_resolves_channel_id_from_url_without_network_access() -> None:
    result = resolve_channel_input("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")

    assert result.kind is ChannelInputKind.CHANNEL_ID
    assert result.normalized == "UCabcdefghijklmnopqrstuv"
    assert result.lookup_parameter == "id"
    assert result.requires_search is False


def test_resolves_handle_from_direct_input_and_url() -> None:
    direct = resolve_channel_input("@GoogleDevelopers")
    url = resolve_channel_input("youtube.com/@GoogleDevelopers/videos")

    assert direct.kind is ChannelInputKind.HANDLE
    assert direct.lookup_parameter == "forHandle"
    assert url == direct


def test_resolves_unicode_handle_from_direct_input_and_encoded_url() -> None:
    direct = resolve_channel_input("@우정잉")
    encoded_url = resolve_channel_input("https://youtube.com/@%EC%9A%B0%EC%A0%95%EC%9E%89/videos")

    assert direct.kind is ChannelInputKind.HANDLE
    assert direct.normalized == "@우정잉"
    assert direct.lookup_parameter == "forHandle"
    assert encoded_url == direct


@pytest.mark.parametrize("value", ["@", "@ invalid", "@.leading", "@trailing-", "@handle/path"])
def test_rejects_malformed_unicode_handle(value: str) -> None:
    with pytest.raises(ChannelInputError):
        resolve_channel_input(value)


def test_legacy_and_ambiguous_names_are_distinguished() -> None:
    legacy = resolve_channel_input("https://youtube.com/user/GoogleDevelopers")
    ambiguous = resolve_channel_input("Google Developers")

    assert legacy.kind is ChannelInputKind.LEGACY_USERNAME
    assert ambiguous.kind is ChannelInputKind.AMBIGUOUS_NAME
    assert ambiguous.requires_search is True


def test_rejects_non_youtube_url() -> None:
    with pytest.raises(ChannelInputError):
        resolve_channel_input("https://example.com/channel/UCabcdefghijklmnopqrstuv")
