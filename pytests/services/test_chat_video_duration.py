"""只读 MP4 盒子头拿时长：Range 请求沿盒子走，不下载整段、不连真实网络。

QQ 视频链接会过期且无法重取，是否交给模型必须先知道时长；盒子头足够回答
这件事——每个盒子读 16 字节头，遇到 ``moov`` 整块读下解析 ``mvhd``，
请求数与文件大小无关。传输层用 ``httpx.MockTransport`` 替身。
"""

from __future__ import annotations

import struct

import httpx
import pytest

from src.core.services.media.chat_video import (
    VideoDurationUnreadableError,
    read_mp4_duration_seconds,
)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


def _large_box_header(kind: bytes, payload_size: int) -> bytes:
    """``size == 1`` 的 64 位长度盒子头；载荷本身不参与盒子头读取。"""
    return struct.pack('>I4sQ', 1, kind, 16 + payload_size)


def _mvhd(timescale: int, duration: int, version: int = 0) -> bytes:
    if version == 1:
        payload = (
            b'\x01\x00\x00\x00' + b'\x00' * 16
            + struct.pack('>IQ', timescale, duration) + b'\x00' * 80
        )
    else:
        payload = (
            b'\x00\x00\x00\x00' + b'\x00' * 8
            + struct.pack('>II', timescale, duration) + b'\x00' * 80
        )
    return _box(b'mvhd', payload)


class _FakeFileServer:
    """按 Range 头从字节串切片应答的虚拟文件服务器，逐个记录请求区间。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.requests: list[tuple[int, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        range_header = request.headers.get('range', '')
        start_text, end_text = range_header.removeprefix('bytes=').split('-')
        start, end = int(start_text), int(end_text)
        self.requests.append((start, end))
        return httpx.Response(
            206,
            content=self.content[start:end + 1],
            headers={'Content-Range': f'bytes {start}-{end}/{len(self.content)}'},
        )


async def _duration_of(server: _FakeFileServer) -> float:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        return await read_mp4_duration_seconds('https://multimedia.nt.qq.com.cn/download?rkey=x', http)


@pytest.mark.asyncio
async def test_moov_at_head_reads_mvhd_v0() -> None:
    content = _box(b'ftyp', b'isom\x00\x00\x00\x00') + _box(b'moov', _mvhd(1000, 18000))
    server = _FakeFileServer(content)

    assert await _duration_of(server) == 18.0
    assert len(server.requests) <= 3


@pytest.mark.asyncio
async def test_moov_after_mdat_reads_mvhd_v1() -> None:
    content = (
        _box(b'ftyp', b'isom\x00\x00\x00\x00')
        + _box(b'free', b'\x00' * 8)
        + _box(b'mdat', b'\x00' * 64)
        + _box(b'moov', _mvhd(600, 10500, version=1))
    )
    server = _FakeFileServer(content)

    assert await _duration_of(server) == 17.5


@pytest.mark.asyncio
async def test_64bit_box_length_is_walked() -> None:
    content = (
        _box(b'ftyp', b'isom\x00\x00\x00\x00')
        + _large_box_header(b'mdat', 32) + b'\x00' * 32
        + _box(b'moov', _mvhd(1000, 5000))
    )
    server = _FakeFileServer(content)

    assert await _duration_of(server) == 5.0


@pytest.mark.asyncio
async def test_size_zero_box_runs_to_end_of_file() -> None:
    moov_payload = _mvhd(1000, 8000)
    content = (
        _box(b'ftyp', b'isom\x00\x00\x00\x00')
        + struct.pack('>I4s', 0, b'moov') + moov_payload
    )
    server = _FakeFileServer(content)

    assert await _duration_of(server) == 8.0


@pytest.mark.asyncio
async def test_request_count_does_not_grow_with_file_size() -> None:
    small = (
        _box(b'ftyp', b'isom\x00\x00\x00\x00')
        + _box(b'free', b'\x00' * 8)
        + _box(b'mdat', b'\x00' * 128)
        + _box(b'moov', _mvhd(600, 10500, version=1))
    )
    big = (
        _box(b'ftyp', b'isom\x00\x00\x00\x00')
        + _box(b'free', b'\x00' * 8)
        + _box(b'mdat', b'\x00' * (50 * 1024 * 1024))
        + _box(b'moov', _mvhd(600, 10500, version=1))
    )
    small_server = _FakeFileServer(small)
    big_server = _FakeFileServer(big)

    assert await _duration_of(small_server) == 17.5
    assert await _duration_of(big_server) == 17.5
    assert len(small_server.requests) == len(big_server.requests)


@pytest.mark.asyncio
async def test_non_mp4_bytes_are_rejected() -> None:
    server = _FakeFileServer(b'this is not an mp4 file at all, just plain text')

    with pytest.raises(VideoDurationUnreadableError):
        await _duration_of(server)


@pytest.mark.asyncio
async def test_expired_link_is_unreadable() -> None:
    def expired(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"retcode":-5503007,"retmsg":"download url has expired"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(expired)) as http:
        with pytest.raises(VideoDurationUnreadableError):
            await read_mp4_duration_seconds('https://multimedia.nt.qq.com.cn/download?rkey=x', http)


@pytest.mark.asyncio
async def test_moov_without_mvhd_is_rejected() -> None:
    content = _box(b'ftyp', b'isom\x00\x00\x00\x00') + _box(b'moov', _box(b'free', b'\x00' * 4))
    server = _FakeFileServer(content)

    with pytest.raises(VideoDurationUnreadableError):
        await _duration_of(server)
