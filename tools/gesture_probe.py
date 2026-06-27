"""Gating probe: verify webcam FPS >= 20 and that open-palm vs pinch separate
cleanly from MediaPipe's 21 hand landmarks.

Run for ~10 seconds, then print a summary. No window display (headless).
Press Ctrl+C to stop early.
"""

import os
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

import time
import sys

import cv2
import mediapipe as mp
import numpy as np

mp_hands = mp.solutions.hands
LANDMARK = mp_hands.HandLandmark

# Finger tip / pip / mcp indices
TIPS = [LANDMARK.INDEX_FINGER_TIP, LANDMARK.MIDDLE_FINGER_TIP,
        LANDMARK.RING_FINGER_TIP, LANDMARK.PINKY_TIP]
PIPS = [LANDMARK.INDEX_FINGER_PIP, LANDMARK.MIDDLE_FINGER_PIP,
        LANDMARK.RING_FINGER_PIP, LANDMARK.PINKY_PIP]
MCPS = [LANDMARK.INDEX_FINGER_MCP, LANDMARK.MIDDLE_FINGER_MCP,
        LANDMARK.RING_FINGER_MCP, LANDMARK.PINKY_MCP]


def _finger_extended(lm, tip_idx, pip_idx):
    return lm[tip_idx].y < lm[pip_idx].y


def _thumb_extended(lm):
    return lm[LANDMARK.THUMB_TIP].y < lm[LANDMARK.THUMB_IP].y


def _palm_size(lm):
    wrist = np.array([lm[LANDMARK.WRIST].x, lm[LANDMARK.WRIST].y])
    mid_mcp = np.array([lm[LANDMARK.MIDDLE_FINGER_MCP].x, lm[LANDMARK.MIDDLE_FINGER_MCP].y])
    return float(np.linalg.norm(mid_mcp - wrist)) + 1e-6


def _pinch_ratio(lm):
    thumb_tip = np.array([lm[LANDMARK.THUMB_TIP].x, lm[LANDMARK.THUMB_TIP].y])
    index_tip = np.array([lm[LANDMARK.INDEX_FINGER_TIP].x, lm[LANDMARK.INDEX_FINGER_TIP].y])
    return float(np.linalg.norm(index_tip - thumb_tip)) / _palm_size(lm)


def classify_pose(lm):
    fingers = [_finger_extended(lm, t, p) for t, p in zip(TIPS, PIPS)]
    thumb = _thumb_extended(lm)
    n_extended = sum(fingers) + int(thumb)
    pinch = _pinch_ratio(lm)

    if n_extended == 5:
        return "OPEN_PALM"
    if n_extended == 0:
        return "FIST"
    if fingers[0] and fingers[1] and not fingers[2] and not fingers[3] and not thumb:
        return "PEACE"
    if thumb and fingers[3] and not fingers[0] and not fingers[1] and not fingers[2]:
        return "SHAKA"
    return "OTHER"


def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Could not open camera 0")
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)

    actual_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    actual_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    print(f"Camera opened: {int(actual_w)}x{int(actual_h)}")

    hands = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        min_detection_confidence=0.6,
        min_tracking_confidence=0.5,
    )

    frame_count = 0
    detect_count = 0
    pose_counts = {}
    start = time.time()
    duration = 10.0

    print(f"Running for {duration:.0f}s — show your hand in various poses.")
    print("Poses tested: OPEN_PALM, FIST, PEACE (index+middle), SHAKA (thumb+pinky)")
    print("-" * 60)

    last_pose = None
    last_pose_start = 0.0

    try:
        while time.time() - start < duration:
            ret, frame = cap.read()
            if not ret:
                print("WARNING: frame read failed")
                time.sleep(0.01)
                continue

            frame_count += 1
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = hands.process(rgb)

            if result.multi_hand_landmarks:
                detect_count += 1
                lm = result.multi_hand_landmarks[0].landmark
                pose = classify_pose(lm)
                pose_counts[pose] = pose_counts.get(pose, 0) + 1

                now = time.time()
                if pose != last_pose:
                    if last_pose is not None:
                        held_ms = (now - last_pose_start) * 1000
                        print(f"  {last_pose:12s} held {held_ms:5.0f}ms")
                    last_pose = pose
                    last_pose_start = now

            elapsed = time.time() - start
            if frame_count % 30 == 0:
                fps = frame_count / elapsed
                print(f"  t={elapsed:.1f}s  frames={frame_count}  fps={fps:.1f}  "
                      f"detections={detect_count}  poses={pose_counts}")

    except KeyboardInterrupt:
        print("\nStopped early.")

    hands.close()
    cap.release()

    elapsed = time.time() - start
    fps = frame_count / elapsed if elapsed > 0 else 0

    print("\n" + "=" * 60)
    print(f"RESULT: {frame_count} frames in {elapsed:.1f}s = {fps:.1f} FPS")
    print(f"Hand detected in {detect_count}/{frame_count} frames "
          f"({100*detect_count/max(frame_count,1):.0f}%)")
    print(f"Pose breakdown: {pose_counts}")

    if fps >= 20:
        print("PASS: FPS >= 20")
    else:
        print(f"FAIL: FPS {fps:.1f} < 20 — gesture lane may be unreliable")

    poses_seen = set(pose_counts.keys())
    distinct = poses_seen - {"OTHER"}
    print(f"Distinct non-OTHER poses seen: {distinct}")
    if len(distinct) >= 2:
        print("PASS: Multiple distinct poses captured")
    else:
        print("INFO: Could not verify pose separation (show open-palm AND fist)")


if __name__ == "__main__":
    main()
