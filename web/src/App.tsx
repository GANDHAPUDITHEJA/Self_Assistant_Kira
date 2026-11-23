import { FormEvent, useEffect, useRef, useState } from "react";

type Role = "user" | "assistant";
type AssistantState = "idle" | "listening" | "thinking" | "acting" | "speaking" | "ready" | "error";

type Turn = { id: string; role: Role; text: string; createdAt: string };
type SearchResult = { index: number; name: string; kind: "file" | "folder"; path: string };
type FolderScope = { name: string | null; path: string | null };

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
  const [searchText, setSearchText] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchMessage, setSearchMessage] = useState("");
  const [searching, setSearching] = useState(false);
  const [folderScope, setFolderScope] = useState<FolderScope>({ name: null, path: null });
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/folder/status")
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!cancelled && data) setFolderScope(data);
      })
      .catch(() => undefined);
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
      if (payload.event === "folder.changed") {
        setFolderScope({ name: data.name ?? null, path: data.path ?? null });
      }
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
    const folderResponse = await fetch("/api/folder/status");
    if (folderResponse.ok) setFolderScope(await folderResponse.json());
  }

  async function toggleMic() {
    if (state === "listening") {
      await fetch("/api/voice/cancel", { method: "POST" });
      return;
    }
    const statusResponse = await fetch("/api/voice/status");
    if (statusResponse.ok) {
      const status = await statusResponse.json();
      if (!status.available) {
        setState("error");
        setDetail(status.error ? `Microphone unavailable: ${status.error}` : "Microphone unavailable");
        return;
      }
    }
    setState("listening");
    setDetail("Listening");
    const response = await fetch("/api/voice/start", { method: "POST" });
    if (!response.ok) {
      setState("error");
      setDetail("Could not start voice");
    }
  }

  async function answerConfirmation(approved: boolean) {
    await fetch("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    });
    setConfirmation(null);
  }

  async function submitSearch(event: FormEvent) {
    event.preventDefault();
    const value = searchText.trim();
    if (!value) return;
    setSearching(true);
    setSearchMessage("");
    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: value }),
      });
      if (!response.ok) throw new Error("Search failed");
      const data = await response.json();
      setSearchResults(data.results ?? []);
      setFolderScope(data.scope ?? { name: null, path: null });
      setSearchMessage(data.results?.length ? `${data.results.length} result(s) found` : "No matching files or folders");
    } catch {
      setSearchResults([]);
      setSearchMessage("Could not search files");
    } finally {
      setSearching(false);
    }
  }

  async function openSearchResult(index: number) {
    const response = await fetch("/api/search/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index }),
    });
    if (!response.ok) return;
    setSearchResults([]);
    setSearchMessage("Opened selected item");
    const folderResponse = await fetch("/api/folder/status");
    if (folderResponse.ok) setFolderScope(await folderResponse.json());
  }

  return (
    <main className={`app state-${state}`}>
      <header className="topbar">
        <div className="brand"><span className="brand-mark" />Kira</div>
        <form className="document-search" onSubmit={submitSearch}>
          {folderScope.name && (
            <span className="search-scope" title={folderScope.path ?? undefined}>
              {folderScope.name}
            </span>
          )}
          <input
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            placeholder="Search files or folders"
            aria-label="Search Documents"
          />
          <button type="submit" disabled={!searchText.trim() || searching} aria-label="Search">
            Search
          </button>
        </form>
        <span className="status-dot" aria-label={detail} />
      </header>

      {(searchResults.length > 0 || searchMessage) && (
        <section className="search-results" aria-live="polite">
          <div className="search-summary">
            {folderScope.name ? `Searching in ${folderScope.name}` : "Search results"}
            {searchMessage ? ` - ${searchMessage}` : ""}
          </div>
          {searchResults.map((result) => (
            <button className="search-result" key={result.path} onClick={() => openSearchResult(result.index)}>
              <span className="result-number">{result.index}</span>
              <span className="result-info">
                <strong>{result.name}</strong>
                <small>{result.kind}</small>
              </span>
              <span className="open-label">Open</span>
            </button>
          ))}
        </section>
      )}

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
