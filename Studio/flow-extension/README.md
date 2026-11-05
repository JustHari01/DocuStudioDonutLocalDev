# Flow Agent Extension for Microsoft Edge / Chromium

Microsoft Edge and Chromium bridge for [kodelyx/flow-agent](https://github.com/kodelyx/flow-agent). It connects a logged-in Google Flow tab to the local Flow Agent backend.

## Features

- Project-backed text-to-image mode that drives the visible Google Flow UI
- Automatically creates a Flow project when no project is open
- Generated images remain visible in the signed-in Google Flow account
- Background/minimized-tab operation with service-worker timing, renderer wake
  pulses and trusted Edge keyboard input; it never steals the active tab
- Versioned local selector catalog: no third-party selector/configuration server
- Controlled one-time Flow UI session repair that preserves Google sign-in
  cookies and never exports cookie values
- Explicit CAPTCHA, policy, quota and rate-limit detection (no verification bypass)
- Fail-closed Nano Banana 2 Lite selection with exactly one image per prompt
  when the selected model reaches its daily image limit; the same prompt is
  retried and the job stops cleanly if both models are exhausted
- Live backend health and Flow credit status
- Premium light-mode side panel opened directly from the extension icon
- Quick image and video generation with persistent history
- Nano Banana 2 Lite as the only image model; one image per prompt
- Native Edge dropdowns for image model, YouTube/Shorts aspect ratio, and video duration
- Remembers the last quick-generation settings
- Compact agent monitoring, token refresh, and Flow controls

## Install

1. Start the backend with `flow`.
2. Open `edge://extensions` and enable **Developer mode**.
3. Click **Load unpacked** and select this `flow-extension` folder (or the prepared `Flow-Agent-Edge` distribution folder).
4. Open <https://flow.google.com/>, sign in, and keep the tab open.
5. Click the extension icon to open Flow Agent in Edge's side panel.

The side panel does not need to remain open while DocuRandom Studio is running.
The Flow tab may stay in the background and Edge may be minimized, but Edge must
remain open. DOM polling is clocked by the extension worker so changing tabs or
minimizing Edge does not slow the automation timer. If Google requests login or
CAPTCHA verification, open the Flow tab, complete it manually, and resume the
workspace task.

## Image models

- **Nano Banana 2 Lite** (`harbor_seal`) - fixed model, exactly one image per prompt.

The model selector applies to image generation. Video generation uses Flow Agent's duration-specific video model.

Main documentation: [Flow Agent README](../README.md)
