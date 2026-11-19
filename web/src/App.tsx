import { FormEvent, useEffect, useRef, useState } from "react";

type Role = "user" | "assistant";
type AssistantState = "idle" | "listening" | "thinking" | "acting" | "speaking" | "ready" | "error";

type Turn = { id: string; role: Role; text: string; createdAt: string };

const stateLabel: Record<AssistantState, string> = {
  idle: "Say Hey Kira",
  listening: "Listening",
  thinking: "Thinking",
  acting: "Working",
  speaking: "Speaking",
  ready: "Ready",
  error: "Something went wrong",
};

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [state, setState] = useState<AssistantState>("idle");
  const [detail, setDetail] = useState("Say Hey Kira");
  const [confirmation, setConfirmation] = useState<{ id: string; question: string } | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/history")
      .then((response) => {
        if (!response.ok) throw new Error("Could not load conversation");
        return response.json();
      })
      .then((data) => {
        if (!cancelled) setTurns(data.turns ?? []);
      })
      .catch(() => {
        if (!cancelled) setTurns([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const events = new EventSource("/api/events");
    events.onmessage = (message) => {
      const payload = JSON.parse(message.data);
      const data = payload.data ?? {};
      if (payload.event === "state.changed") {
        setState(data.state ?? "ready");
        setDetail(data.detail ?? stateLabel[data.state as AssistantState] ?? "Ready");
      }
      if (payload.event === "transcript.final") {
        setTurns((old) => [...old, { id: crypto.randomUUID(), role: "user", text: data.text, createdAt: new Date().toISOString() }]);
      }
      if (payload.event === "response.final") {
        setTurns((old) => [...old, { id: crypto.randomUUID(), role: "assistant", text: data.text, createdAt: new Date().toISOString() }]);
      }
      if (payload.event === "confirmation.requested") setConfirmation({ id: data.id, question: data.question });
    };
    return () => {
      events.close();
    };
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, detail]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const value = text.trim();
    if (!value) return;
    setText("");
    setTurns((old) => [...old, { id: crypto.randomUUID(), role: "user", text: value, createdAt: new Date().toISOString() }]);
    setState("thinking");
    setDetail("Thinking");
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: value }),
    });
    if (!response.ok) setState("error");
  }

  async function toggleMic() {
    if (state === "listening") {
      await fetch("/api/voice/cancel", { method: "POST" });
      return;
    }
    setState("listening");
    setDetail("Listening");
    await fetch("/api/voice/start", { method: "POST" });
  }

  async function answerConfirmation(approved: boolean) {
    await fetch("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    });
    setConfirmation(null);
  }

  return (
    <main className={`app state-${state}`}>
      <header className="topbar">
        <div className="brand"><span className="brand-mark" />Kira</div>
        <span className="status-dot" aria-label={detail} />
      </header>

      <section className="conversation" aria-label="Conversation">
        {turns.length === 0 && (
          <div className="empty">
            <h1>How can I help?</h1>
          </div>
        )}
        {turns.map((turn) => (
          <article className={`turn ${turn.role}`} key={turn.id}>
            <div className="bubble">{turn.text}</div>
          </article>
        ))}
        {confirmation && (
          <aside className="confirmation" role="alert">
            <p>{confirmation.question}</p>
            <div>
              <button onClick={() => answerConfirmation(false)}>Cancel</button>
              <button className="confirm" onClick={() => answerConfirmation(true)}>Confirm</button>
            </div>
          </aside>
        )}
        <div ref={endRef} />
      </section>

      <footer className="bottom">
        <button className="mic" onClick={toggleMic} aria-label={state === "listening" ? "Stop listening" : "Start listening"}>
          <span className="mic-ring" />
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2Z" />
          </svg>
        </button>
        <div className="status" role="status" aria-live="polite">{detail}</div>
        <form className="composer" onSubmit={submit}>
          <input
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Ask Kira"
            aria-label="Message Kira"
            maxLength={2000}
          />
          <button type="submit" aria-label="Send message" disabled={!text.trim()}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 20v-6l15-2-15-2V4l18 8-18 8Z" /></svg>
          </button>
        </form>
      </footer>
    </main>
  );
}
