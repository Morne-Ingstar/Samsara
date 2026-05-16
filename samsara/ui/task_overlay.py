"""pywebview overlay for the local task list."""

import json
import threading

import webview


class TaskOverlay:
    def __init__(self):
        self._window = None
        self._thread = None

    def show(self, tasks):
        """Open the overlay. If already open, refresh and bring to front."""
        if self._window is not None:
            try:
                self._window.evaluate_js(
                    f"updateTasks({json.dumps(tasks)})"
                )
                self._window.on_top = True
                return
            except Exception:
                self._window = None

        self._thread = threading.Thread(
            target=self._create_window,
            args=(tasks,),
            daemon=True,
            name="task-overlay",
        )
        self._thread.start()

    def _create_window(self, tasks):
        html = self._build_html(tasks)
        self._window = webview.create_window(
            "Tasks",
            html=html,
            width=340,
            height=460,
            resizable=True,
            on_top=True,
            frameless=False,
            background_color="#0b0e14",
        )
        try:
            webview.start(gui="edgechromium")
        except Exception:
            try:
                webview.start()
            except Exception as e:
                print(f"[TASKS] Could not open overlay: {e}")
        self._window = None

    def hide(self):
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None

    def refresh(self, tasks):
        if self._window is not None:
            try:
                self._window.evaluate_js(
                    f"updateTasks({json.dumps(tasks)})"
                )
            except Exception:
                pass

    def _build_html(self, tasks):
        tasks_json = json.dumps(tasks)
        return (
            '<!DOCTYPE html>'
            '<html>'
            '<head>'
            '<style>'
            '* { box-sizing: border-box; margin: 0; padding: 0; }'
            'body {'
            '    background: #0b0e14;'
            '    color: #E8E8EA;'
            '    font-family: "Segoe UI", -apple-system, sans-serif;'
            '    font-size: 14px;'
            '    line-height: 1.5;'
            '    padding: 16px;'
            '    user-select: none;'
            '}'
            'h1 {'
            '    font-size: 16px;'
            '    font-weight: 600;'
            '    color: #5EEAD4;'
            '    margin-bottom: 16px;'
            '    padding-bottom: 8px;'
            '    border-bottom: 1px solid rgba(255,255,255,0.08);'
            '}'
            '.task {'
            '    display: flex;'
            '    align-items: flex-start;'
            '    gap: 10px;'
            '    padding: 8px 4px;'
            '    border-bottom: 1px solid rgba(255,255,255,0.04);'
            '    transition: background 0.15s;'
            '}'
            '.task:hover { background: rgba(255,255,255,0.03); }'
            '.task-num {'
            '    color: #5EEAD4;'
            '    font-weight: 600;'
            '    min-width: 20px;'
            '    font-size: 13px;'
            '    padding-top: 1px;'
            '}'
            '.task-text { flex: 1; word-break: break-word; }'
            '.task-completed .task-text {'
            '    text-decoration: line-through;'
            '    color: #5A5A62;'
            '}'
            '.task-completed .task-num { color: #5A5A62; }'
            '.section-label {'
            '    font-size: 11px;'
            '    font-weight: 600;'
            '    text-transform: uppercase;'
            '    letter-spacing: 0.06em;'
            '    color: #5A5A62;'
            '    margin-top: 16px;'
            '    margin-bottom: 6px;'
            '}'
            '.empty {'
            '    color: #5A5A62;'
            '    font-style: italic;'
            '    padding: 20px 0;'
            '    text-align: center;'
            '}'
            '.count {'
            '    font-size: 12px;'
            '    color: #5A5A62;'
            '    margin-top: 12px;'
            '    padding-top: 8px;'
            '    border-top: 1px solid rgba(255,255,255,0.06);'
            '}'
            '</style>'
            '</head>'
            '<body>'
            '<h1>Tasks</h1>'
            '<div id="task-list"></div>'
            '<script>'
            'let currentTasks = ' + tasks_json + ';'
            'function updateTasks(tasks) { currentTasks = tasks; render(); }'
            'function render() {'
            '    const container = document.getElementById("task-list");'
            '    const active = currentTasks.filter(t => !t.completed);'
            '    const completed = currentTasks.filter(t => t.completed);'
            '    let html = "";'
            '    if (active.length === 0 && completed.length === 0) {'
            '        html = "<div class=\\"empty\\">No tasks yet. Say \\"add to list\\" to create one.</div>";'
            '    } else {'
            '        active.forEach((t, i) => {'
            '            html += "<div class=\\"task\\">";'
            '            html += "<span class=\\"task-num\\">" + (i + 1) + ".</span>";'
            '            html += "<span class=\\"task-text\\">" + escapeHtml(t.text) + "</span>";'
            '            html += "</div>";'
            '        });'
            '        if (completed.length > 0) {'
            '            html += "<div class=\\"section-label\\">Completed</div>";'
            '            completed.forEach(t => {'
            '                html += "<div class=\\"task task-completed\\">";'
            '                html += "<span class=\\"task-num\\">&#10003;</span>";'
            '                html += "<span class=\\"task-text\\">" + escapeHtml(t.text) + "</span>";'
            '                html += "</div>";'
            '            });'
            '        }'
            '        const done = completed.length;'
            '        const total = currentTasks.length;'
            '        html += "<div class=\\"count\\">" + done + " of " + total + " completed</div>";'
            '    }'
            '    container.innerHTML = html;'
            '}'
            'function escapeHtml(str) {'
            '    const div = document.createElement("div");'
            '    div.textContent = str;'
            '    return div.innerHTML;'
            '}'
            'render();'
            '</script>'
            '</body>'
            '</html>'
        )
