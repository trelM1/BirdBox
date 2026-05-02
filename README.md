# BirdBox

## Demo 
[Visit BirdBox](https://hheresjohnny.github.io/BirdBox/)

## What was our inspiration?

Over 250 million people worldwide live with visual impairment, yet most assistive navigation tools are either expensive, bulky, or require specialized hardware. We wanted to build something that anyone could use right now, with just the phone already in their pocket. BirdBox was born from a simple question: what if your phone could be your eyes?

## What does our project do?

BirdBox is a real-time AI-powered navigation assistant for visually impaired users. You hold your phone in front of you and it continuously analyzes your surroundings through the camera. If it spots a hazard, a step, a door, or a person in your path, it speaks a warning aloud and vibrates with a distinct haptic pattern so you always know the severity at a glance. Beyond obstacle detection, BirdBox includes full voice-controlled navigation. Say "Hey BirdBox, take me to the nearest Burger King," and it searches nearby, reads out your options, and guides you turn by turn with spoken directions and haptic cues at every step. Meanwhile, a companion web dashboard lets a caregiver, family member, or support worker watch your live location on a map, see every hazard the AI detects in real time, and track your full movement history all streamed online the moment something is detected.

## How did we build it?

Frontend (mobile): An HTML/JS Web App that runs entirely in the browser, no install required. It captures camera frames, handles detection via the Web Speech API, and drives haptic feedback.
AI vision: Each frame is sent to the Anthropic Claude API, which analyzes the scene and returns a hazard level (safe, warning, or urgent) with a description spoken aloud.
Voice: ElevenLabs provides natural, human-sounding voice output for a more calming and intelligible experience than browser TTS alone.
Navigation: Google Maps' Places and Directions APIs powers the location search and directions, delivered entirely through voice and haptics.
Backend: A FastAPI server that manages all API orchestration and reverse geocoding with a WebSocket endpoint that streams live location and hazard data to the dashboard.
Data: Snowflake stores the event log, every scan result, location update, and hazard detection which gives us a queryable history of each session.
Dashboard: A standalone HTML page connects via WebSocket and relays the user's live position on a Google Maps dark-mode interface alongside a obstacle log.

## What were some challenges?

Getting ElevenLabs, Snowflake, and the Anthropic API to all work together effectively through a single FastAPI backend was our biggest technical obstacle. Each service has its own authentication model and rate limits, and coordinating them in real time, where a slow API call can mean a missed hazard warning, required careful async handling and fallback logic. We also spent significant time tuning the scan interval and prompt design to make Claude's hazard descriptions specific enough to be spoken quickly without losing useful detail.

## What are we proud of?

The moment when the AI correctly identified a hazard in a live camera feed with the phone vibrating was the moment where we felt that our time spent was not a complete waste. Building a system that works entirely through sound and touch, with no screen required, and watching it actually guide someone through a space felt meaningful. We are also proud of how lightweight the final product is, being just a URL.

## What did we learn?

Designing for users who cannot look at a screen forces you to rethink every assumption about UI. Every piece of information has to be communicated through voice, timing, or touch, which made us much more careful about what the AI says, when it says it, and how long it takes. From this project, we gained crucial experience from balancing multiple AI APIs to ensure there’s no waiting around for results.

## What's next?

In the near future, we plan to make the AI smarter where it is better at distinguishing between hazard types and estimating distances. Longer term, we are looking at adding user profiles in Snowflake so the system learns your frequently visited routes.
