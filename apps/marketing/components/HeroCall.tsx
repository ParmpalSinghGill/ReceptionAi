"use client";

import * as React from "react";
import {
  ConversationCard,
  ConversationResult,
  TranscriptRow,
  Waveform,
} from "@receptionai/ui";

/**
 * The hero's conversation card: a scripted, representative call.
 *
 * The amplitudes are a fixed, hand-authored capture rather than a random or
 * animated sequence — DESIGN.md §5 says the waveform's heights are driven by
 * real amplitude and must never fake an animation implying audio that isn't
 * there. This is a still frame of one call, and it does not move.
 */
const CALL_AMPLITUDES = [
  0.12, 0.18, 0.31, 0.44, 0.62, 0.78, 0.66, 0.49, 0.35, 0.28, 0.41, 0.58,
  0.72, 0.85, 0.7, 0.53, 0.38, 0.26, 0.19, 0.24, 0.37, 0.52, 0.68, 0.81,
  0.74, 0.59, 0.43, 0.3, 0.22, 0.16, 0.25, 0.39, 0.55, 0.7, 0.63, 0.47,
  0.33, 0.21, 0.15, 0.11, 0.18, 0.29, 0.42, 0.36, 0.24, 0.17, 0.13, 0.1,
];

export function HeroCall() {
  return (
    <ConversationCard
      status="Incoming call"
      elapsed="0:24"
      live
      waveform={
        <Waveform
          amplitudes={CALL_AMPLITUDES}
          height={40}
          label="Audio waveform of the call in progress"
        />
      }
      footer={
        <ConversationResult>
          Confirmed and written to your calendar
        </ConversationResult>
      }
    >
      <TranscriptRow speaker="Caller" tone="caller">
        Hi — I chipped a tooth this morning and it&rsquo;s pretty sore. Can
        anyone see me today?
      </TranscriptRow>
      <TranscriptRow speaker="Desk" tone="desk">
        I&rsquo;m sorry, that sounds painful. I can get you in with Dr. Achebe
        at 3:40 this afternoon, or 8:15 tomorrow morning if you&rsquo;d rather
        not rush.
      </TranscriptRow>
      <TranscriptRow speaker="Caller" tone="caller">
        Today please. 3:40 works.
      </TranscriptRow>
      <TranscriptRow speaker="Desk" tone="desk">
        Booked — 3:40 today with Dr. Achebe, and I&rsquo;ve texted you the
        address and a note about what to do if the pain worsens before then.
      </TranscriptRow>
    </ConversationCard>
  );
}
