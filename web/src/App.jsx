import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const views = ["Bind", "Status", "Teleop", "Record", "Review"];

function App() {
  const [view, setView] = useState("Bind");
  const [output, setOutput] = useState({});

  async function call(path, options) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const payload = await response.json();
    setOutput(payload);
  }

  return (
    <main className="shell">
      <aside className="steps">
        <strong>SOARM Studio</strong>
        {views.map((item, index) => (
          <button
            key={item}
            className={item === view ? "active" : ""}
            onClick={() => setView(item)}
          >
            {index + 1} {item}
          </button>
        ))}
      </aside>
      <section className="workspace">
        <header>
          <h1>{view}</h1>
          <button onClick={() => call("/api/preflight?overwrite=true")}>Refresh</button>
        </header>
        <section className="panel">
          <div className="actions">
            <button onClick={() => call("/api/bindings")}>Bindings</button>
            <button onClick={() => call("/api/status")}>Status</button>
            <button onClick={() => call("/api/preflight?overwrite=true")}>Preflight</button>
            <button
              onClick={() =>
                call("/api/calibrate", {
                  method: "POST",
                  body: JSON.stringify({ role: "both" }),
                })
              }
            >
              Calibrate
            </button>
            <button
              onClick={() =>
                call("/api/teleop", {
                  method: "POST",
                  body: JSON.stringify({ seconds: 2 }),
                })
              }
            >
              Teleop
            </button>
            <button
              onClick={() =>
                call("/api/record", {
                  method: "POST",
                  body: JSON.stringify({
                    task: "vite recording",
                    seconds: 2,
                    episodes: 1,
                    overwrite: true,
                  }),
                })
              }
            >
              Record
            </button>
          </div>
          <pre>{JSON.stringify(output, null, 2)}</pre>
        </section>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<App />);
