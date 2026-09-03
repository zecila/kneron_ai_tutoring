#!/usr/bin/env python3
"""Post-restart smoke test for backend TTS and LiveTalking WebRTC."""

import argparse
import asyncio
import io
import time
import wave

import requests
from aiortc import RTCPeerConnection, RTCSessionDescription


def wait_for_json(url, timeout):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=3)
            if response.ok:
                return response.json()
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def check_tts(backend_url, count):
    results = []
    for index in range(1, count + 1):
        started = time.monotonic()
        response = requests.post(
            f"{backend_url}/api/tts",
            json={"text": f"Restart smoke test narration number {index}."},
            timeout=120,
        )
        response.raise_for_status()
        with wave.open(io.BytesIO(response.content), "rb") as wav_file:
            if wav_file.getnframes() < 1:
                raise RuntimeError(f"TTS test {index} returned an empty WAV")
            results.append({
                "request": index,
                "seconds": round(time.monotonic() - started, 2),
                "frames": wav_file.getnframes(),
                "rate": wav_file.getframerate(),
            })
    return results


async def check_webrtc(livetalking_url):
    peer = RTCPeerConnection()
    received_tracks = set()

    @peer.on("track")
    def on_track(track):
        received_tracks.add(track.kind)

    peer.addTransceiver("video", direction="recvonly")
    peer.addTransceiver("audio", direction="recvonly")
    await peer.setLocalDescription(await peer.createOffer())

    response = requests.post(
        f"{livetalking_url}/offer",
        json={"sdp": peer.localDescription.sdp, "type": peer.localDescription.type},
        timeout=90,
    )
    response.raise_for_status()
    answer = response.json()
    session_id = answer["sessionid"]
    await peer.setRemoteDescription(
        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
    )

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if peer.connectionState == "connected" and received_tracks == {"audio", "video"}:
            break
        if peer.connectionState == "failed":
            break
        await asyncio.sleep(0.25)

    result = {
        "connection_state": peer.connectionState,
        "tracks": sorted(received_tracks),
        "session_created": bool(session_id),
    }
    await peer.close()
    try:
        requests.post(
            f"{livetalking_url}/close_session",
            json={"sessionid": session_id},
            timeout=10,
        ).raise_for_status()
    except requests.RequestException:
        pass

    if result["connection_state"] != "connected" or result["tracks"] != ["audio", "video"]:
        raise RuntimeError(f"WebRTC smoke test failed: {result}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-url", default="http://127.0.0.1:5000")
    parser.add_argument("--livetalking-url", default="http://127.0.0.1:8010")
    parser.add_argument("--startup-timeout", type=int, default=360)
    parser.add_argument("--tts-count", type=int, default=5)
    args = parser.parse_args()

    backend_health = wait_for_json(f"{args.backend_url}/api/health", args.startup_timeout)
    avatar_health = wait_for_json(f"{args.backend_url}/api/avatar/health", args.startup_timeout)
    tts_results = check_tts(args.backend_url, args.tts_count)
    webrtc_result = asyncio.run(check_webrtc(args.livetalking_url))
    print({
        "backend": backend_health,
        "avatar": avatar_health,
        "tts": tts_results,
        "webrtc": webrtc_result,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
