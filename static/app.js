(function () {
  function appendLine(panel, payload) {
    const line = document.createElement("div");
    line.className = "console-line console-line-" + payload.kind;

    const time = document.createElement("span");
    time.className = "console-time";
    time.textContent = payload.created_at || "now";

    const message = document.createElement("span");
    message.className = "console-message";
    message.textContent = payload.message || "";

    line.appendChild(time);
    line.appendChild(message);
    panel.appendChild(line);
    panel.scrollTop = panel.scrollHeight;
  }

  function updateStatus(payload) {
    const statusNode = document.querySelector("[data-job-status]");
    const hintNode = document.querySelector("[data-job-hint]");

    if (statusNode && payload.status_label) {
      statusNode.textContent = payload.status_label;
      statusNode.className = "status-pill status-" + payload.status;
    }

    if (hintNode && payload.status_hint) {
      hintNode.textContent = payload.status_hint;
    }
  }

  function updateActions(payload) {
    const rawReportLink = document.querySelector("[data-raw-report-link]");

    if (rawReportLink && payload.raw_report_url) {
      rawReportLink.href = payload.raw_report_url;
      rawReportLink.classList.remove("is-hidden");
    }
  }

  function processEvents(panel, payload) {
    updateStatus(payload);
    updateActions(payload);

    (payload.events || []).forEach(function (event) {
      if (event.message) {
        appendLine(panel, event);
      }
    });

    if (typeof payload.event_count !== "undefined") {
      panel.dataset.eventCount = String(payload.event_count);
    }
  }

  function finishIfNeeded(payload, eventSource) {
    if (payload.status === "finished" || payload.status === "failed" || payload.status === "timed_out" ||
        payload.status === "scanner_unavailable" || payload.status === "report_missing" ||
        payload.status === "invalid_report") {
      const hintNode = document.querySelector("[data-job-hint]");

      if (eventSource) {
        eventSource.close();
      }

      updateActions(payload);

      if (hintNode) {
        hintNode.textContent = payload.raw_report_url
          ? "Scan finished. You can download the raw report below."
          : "Scan finished. No raw report file is available for download.";
      }

      return true;
    }

    return false;
  }

  function startPolling(panel) {
    const pollUrl = panel.dataset.pollUrl;
    const hintNode = document.querySelector("[data-job-hint]");

    function pollOnce() {
      const startIndex = panel.dataset.eventCount || "0";
      const url = pollUrl + "?from=" + encodeURIComponent(startIndex);

      fetch(url, { headers: { Accept: "application/json" } })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Polling failed");
          }

          return response.json();
        })
        .then(function (payload) {
          processEvents(panel, payload);

          if (finishIfNeeded(payload, null)) {
            return;
          }

          window.setTimeout(pollOnce, 1500);
        })
        .catch(function () {
          if (hintNode) {
            hintNode.textContent = "Automatic updates are retrying. If this persists, refresh the page once.";
          }

          window.setTimeout(pollOnce, 3000);
        });
    }

    if (hintNode) {
      hintNode.textContent = "Live stream dropped, switching to automatic polling fallback.";
    }

    pollOnce();
  }

  function connectStream(panel) {
    const startIndex = panel.dataset.eventCount || "0";
    const eventsUrl = panel.dataset.eventsUrl + "?from=" + encodeURIComponent(startIndex);
    const eventSource = new EventSource(eventsUrl);
    let fallbackStarted = false;

    ["status", "log", "error", "done"].forEach(function (eventName) {
      eventSource.addEventListener(eventName, function (event) {
        const payload = JSON.parse(event.data);
        processEvents(panel, {
          events: [payload],
          event_count: Number(panel.dataset.eventCount || "0") + 1,
          status: payload.status,
          status_label: payload.status_label,
          status_hint: payload.status_hint,
          result_url: payload.result_url,
          raw_report_url: payload.raw_report_url
        });
        finishIfNeeded(payload, eventSource);
      });
    });

    eventSource.onerror = function () {
      if (!fallbackStarted) {
        fallbackStarted = true;
        eventSource.close();
        startPolling(panel);
      }
    };
  }

  document.addEventListener("DOMContentLoaded", function () {
    const panel = document.querySelector("[data-log-panel]");
    if (panel) {
      connectStream(panel);
    }
  });
})();
