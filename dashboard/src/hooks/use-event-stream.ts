"use client";

import { useEffect, useRef, useCallback, useState } from "react";
import type { StreamEvent, StreamEventData, ConnectionState } from "@/lib/stream-types";

const MAX_EVENTS = 200;
const STREAM_URL = "/api/v1/stream";

const EVENT_TYPES = [
  "session_dispatched",
  "session_started",
  "notification",
  "turn_completed",
  "turn_failed",
  "session_ended",
  "session_killed",
  "retry_scheduled",
  "gap",
  "overflow",
];

export function useEventStream(onEvent?: (event: StreamEvent) => void) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connectionState, setConnectionState] = useState<ConnectionState>("disconnected");
  const sourceRef = useRef<EventSource | null>(null);
  const lastEventIdRef = useRef<number | null>(null);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const connect = useCallback(() => {
    if (sourceRef.current) {
      sourceRef.current.close();
    }

    const url = lastEventIdRef.current != null
      ? `${STREAM_URL}?lastEventId=${lastEventIdRef.current}`
      : STREAM_URL;

    const source = new EventSource(url);
    sourceRef.current = source;
    setConnectionState("connecting");

    source.onopen = () => {
      setConnectionState("connected");
    };

    source.onerror = () => {
      setConnectionState("disconnected");
      // EventSource auto-reconnects — we just track state
    };

    // Register typed event listeners
    for (const eventType of EVENT_TYPES) {
      source.addEventListener(eventType, (e: MessageEvent) => {
        let data: StreamEventData;
        try {
          data = JSON.parse(e.data) as StreamEventData;
        } catch {
          return;
        }

        const id = e.lastEventId ? parseInt(e.lastEventId, 10) : 0;
        if (id > 0) {
          lastEventIdRef.current = id;
        }

        const streamEvent: StreamEvent = {
          id,
          eventType,
          data,
          receivedAt: new Date().toISOString(),
        };

        setEvents((prev) => {
          const next = [streamEvent, ...prev];
          return next.length > MAX_EVENTS ? next.slice(0, MAX_EVENTS) : next;
        });

        onEventRef.current?.(streamEvent);
      });
    }
  }, []);

  const disconnect = useCallback(() => {
    if (sourceRef.current) {
      sourceRef.current.close();
      sourceRef.current = null;
    }
    setConnectionState("disconnected");
  }, []);

  const clearEvents = useCallback(() => {
    setEvents([]);
  }, []);

  useEffect(() => {
    connect();
    return () => {
      disconnect();
    };
  }, [connect, disconnect]);

  return {
    events,
    connectionState,
    clearEvents,
    reconnect: connect,
  };
}
