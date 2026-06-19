const state = {
  view: "setup",
};

const views = {
  setup: "Setup",
  check: "Check",
  teleop: "Teleop",
  record: "Record",
  review: "Review",
};

const $ = (selector) => document.querySelector(selector);

function showView(view) {
  state.view = view;
  $("#viewTitle").textContent = views[view];
  document.querySelectorAll(".step").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.panel === view);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(JSON.stringify(payload, null, 2));
  }
  return payload;
}

function print(target, value) {
  $(target).textContent = JSON.stringify(value, null, 2);
}

function updateQuality(result) {
  const episode = result.episodes?.[result.episodes.length - 1];
  const quality = episode?.quality;
  if (!quality) return;
  $("#stateMetric").textContent = "Saved";
  $("#framesMetric").textContent = String(quality.frames);
  $("#loopMetric").textContent = `${quality.max_loop_latency_ms.toFixed(1)} ms`;
  $("#cameraMetric").textContent = `${quality.max_camera_latency_ms.toFixed(1)} ms`;
}

function renderPreflight(report) {
  const list = $("#preflightList");
  list.innerHTML = "";
  for (const check of report.checks || []) {
    const row = document.createElement("div");
    row.className = `check ${check.ok ? "" : "fail"}`;
    row.innerHTML = `<strong>${check.ok ? "PASS" : "FAIL"}</strong><span>${check.detail}</span>`;
    list.append(row);
  }
  $("#stateMetric").textContent = report.ok ? "Ready" : "Blocked";
}

async function refresh() {
  if (state.view === "setup") {
    print("#bindingsOut", await api("/api/bindings"));
  } else if (state.view === "check") {
    renderPreflight(await api("/api/preflight?overwrite=true"));
  } else if (state.view === "review") {
    print("#inspectOut", await api("/api/dataset/inspect"));
  }
}

document.querySelectorAll(".step").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view));
});

$("#refreshBtn").addEventListener("click", refresh);
$("#verifyBtn").addEventListener("click", async () => print("#bindingsOut", await api("/api/bindings")));
$("#statusBtn").addEventListener("click", async () => print("#statusOut", await api("/api/status")));
$("#preflightBtn").addEventListener("click", async () => {
  renderPreflight(await api("/api/preflight?overwrite=true"));
});
$("#calibrateBtn").addEventListener("click", async () => {
  print(
    "#statusOut",
    await api("/api/calibrate", {
      method: "POST",
      body: JSON.stringify({ role: "both" }),
    }),
  );
});
$("#teleopBtn").addEventListener("click", async () => {
  const seconds = Number($("#teleopSeconds").value);
  print(
    "#teleopOut",
    await api("/api/teleop", {
      method: "POST",
      body: JSON.stringify({ seconds }),
    }),
  );
});
$("#recordBtn").addEventListener("click", async () => {
  const result = await api("/api/record", {
    method: "POST",
    body: JSON.stringify({
      task: $("#taskInput").value,
      seconds: Number($("#recordSeconds").value),
      episodes: Number($("#episodeCount").value),
      overwrite: $("#overwriteInput").checked,
    }),
  });
  print("#recordOut", result);
  updateQuality(result);
});
$("#inspectBtn").addEventListener("click", async () => print("#inspectOut", await api("/api/dataset/inspect")));
$("#validateBtn").addEventListener("click", async () => {
  print("#validateOut", await api("/api/dataset/validate"));
});

refresh().catch((error) => {
  $("#bindingsOut").textContent = error.message;
});
