/* Pobi v2 前端 SPA — 纯 vanilla JS，无构建步骤 */
(() => {
  "use strict";

  const API = "/api/v1";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  // ---- 状态 ----
  const state = {
    token: localStorage.getItem("pobi_token") || null,
    user: null,
    es: null, // EventSource
  };

  // ---- 工具 ----
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }
  function toast(msg, isErr = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (isErr ? " err" : "");
    setTimeout(() => t.classList.add("hidden"), 2600);
  }
  function fmtDate(s) {
    if (!s) return "—";
    const d = new Date(s);
    return isNaN(d) ? String(s) : d.toLocaleString("zh-CN", { hour12: false });
  }

  // ---- API 封装 ----
  async function api(path, { method = "GET", body } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    const res = await fetch(API + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 204) return null;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || (data.errors && data.errors[0]?.msg) || "请求失败";
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  // ---- 鉴权 ----
  async function login(email, password) {
    const data = await api("/auth/login", { method: "POST", body: { email, password } });
    state.token = data.access_token;
    state.user = data.user;
    localStorage.setItem("pobi_token", state.token);
  }
  async function register(payload) {
    const data = await api("/auth/register", { method: "POST", body: payload });
    state.token = data.access_token;
    state.user = data.user;
    localStorage.setItem("pobi_token", state.token);
  }
  async function loadMe() {
    state.user = await api("/auth/me");
  }

  // ---- 登录视图 ----
  function showLogin() {
    $("#login-view").classList.remove("hidden");
    $("#app-view").classList.add("hidden");
  }
  function showApp() {
    $("#login-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    $("#user-info").textContent = state.user
      ? `${state.user.email} · ${state.user.is_admin ? "管理员" : "成员"}`
      : "";
  }

  function bindAuth() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        $$(".tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        const which = tab.dataset.tab;
        $("#login-form").classList.toggle("hidden", which !== "login");
        $("#register-form").classList.toggle("hidden", which !== "register");
      });
    });

    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      $("#auth-error").textContent = "";
      try {
        await login(f.email.value.trim(), f.password.value);
        await enterApp();
      } catch (err) {
        $("#auth-error").textContent = err.message;
      }
    });

    $("#register-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      $("#auth-error").textContent = "";
      try {
        await register({
          email: f.email.value.trim(),
          password: f.password.value,
          full_name: f.full_name.value.trim() || null,
          tenant_slug: f.tenant_slug.value.trim(),
        });
        await enterApp();
      } catch (err) {
        $("#auth-error").textContent = err.message;
      }
    });

    $("#logout-btn").addEventListener("click", () => {
      closeStream();
      state.token = null;
      state.user = null;
      localStorage.removeItem("pobi_token");
      showLogin();
    });
  }

  async function enterApp() {
    try {
      if (!state.user) await loadMe();
    } catch {
      state.token = null;
      localStorage.removeItem("pobi_token");
      showLogin();
      return;
    }
    showApp();
    await loadTasks();
  }

  // ---- 视图切换 ----
  function bindNav() {
    $$(".nav-item").forEach((item) => {
      item.addEventListener("click", () => {
        $$(".nav-item").forEach((n) => n.classList.remove("active"));
        item.classList.add("active");
        const v = item.dataset.view;
        $$(".view").forEach((sec) => sec.classList.add("hidden"));
        $("#view-" + v).classList.remove("hidden");
        if (v === "tasks") loadTasks();
        if (v === "targets") loadTargets();
        if (v === "approvals") loadApprovals();
        if (v === "audit") loadAudit();
      });
    });

    document.addEventListener("click", (e) => {
      const act = e.target.closest("[data-action]");
      if (!act) return;
      const a = act.dataset.action;
      if (a === "new-task") openTaskModal();
      if (a === "new-target") openTargetModal();
      if (a === "refresh-approvals") loadApprovals();
      if (a === "refresh-audit") loadAudit();
    });

    // 模态 / 抽屉关闭
    [$("#modal-root"), $("#drawer-root")].forEach((root) => {
      root.addEventListener("click", (e) => {
        if (e.target.dataset.close !== undefined || e.target.closest("[data-close]")) {
          root.classList.add("hidden");
          closeStream();
        }
      });
    });
  }

  // ---- 模态 / 抽屉 ----
  function openModal(html) {
    $("#modal-body").innerHTML = html;
    $("#modal-root").classList.remove("hidden");
  }
  function closeModal() {
    $("#modal-root").classList.add("hidden");
  }
  function openDrawer(html) {
    $("#drawer-body").innerHTML = html;
    $("#drawer-root").classList.remove("hidden");
  }
  function closeDrawer() {
    $("#drawer-root").classList.add("hidden");
    closeStream();
  }

  // ---- 标签输入 ----
  function bindTagInput(container) {
    const input = $("[data-tag-input]", container);
    if (!input) return [];
    const tags = [];
    const list = $("[data-tag-list]", container);
    const render = () => {
      list.innerHTML = tags
        .map((t, i) => `<span class="chip">${esc(t)}<button data-i="${i}">×</button></span>`)
        .join("");
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        const v = input.value.trim().replace(/,$/, "");
        if (v && !tags.includes(v)) tags.push(v);
        input.value = "";
        render();
      }
    });
    list.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) {
        tags.splice(+b.dataset.i, 1);
        render();
      }
    });
    return tags;
  }

  // ---- 目标 ----
  async function loadTargets() {
    try {
      const list = await api("/targets");
      const el = $("#target-list");
      if (!list.length) {
        el.innerHTML = `<div class="empty">暂无授权目标。点击右上角「新建目标」开始。</div>`;
        return;
      }
      el.innerHTML = list
        .map(
          (t) => `
        <div class="card" data-target="${t.id}">
          <h3>${esc(t.name)}</h3>
          <div class="meta">
            <span>${esc(t.url)}</span>
            <span class="${t.enabled ? "badge st-completed" : "badge st-cancelled"}">${t.enabled ? "启用" : "停用"}</span>
          </div>
          <div class="meta" style="margin-top:6px">
            <span>授权 ${t.in_scope.length} · 排除 ${t.out_of_scope.length}</span>
          </div>
          <div class="desc">${esc(t.description || "")}</div>
        </div>`
        )
        .join("");
      $$("[data-target]", el).forEach((c) =>
        c.addEventListener("click", () => openTargetDetail(c.dataset.target))
      );
    } catch (err) {
      toast(err.message, true);
    }
  }

  function openTargetModal() {
    openModal(`
      <h3>新建授权目标</h3>
      <form id="target-form" class="form-grid">
        <label>名称<input name="name" required placeholder="例如：生产官网" /></label>
        <label>URL<input name="url" required placeholder="https://example.com" /></label>
        <label>描述<textarea name="description" placeholder="可选"></textarea></label>
        <label>授权范围 (回车添加)
          <div class="tag-input">
            <input data-tag-input placeholder="https://example.com/*" />
            <div data-tag-list></div>
          </div>
        </label>
        <label>排除范围 (回车添加)
          <div class="tag-input">
            <input data-tag-input placeholder="/admin/*" />
            <div data-tag-list></div>
          </div>
        </label>
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-close>取消</button>
          <button type="submit" class="btn primary">创建</button>
        </div>
      </form>`);

    const containers = $$(".tag-input", $("#target-form"));
    let inScope = [], outScope = [];
    const collect = () => {
      inScope = bindTagInput(containers[0]);
      outScope = bindTagInput(containers[1]);
    };
    setTimeout(collect, 0);

    $("#target-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api("/targets", {
          method: "POST",
          body: {
            name: f.name.value.trim(),
            url: f.url.value.trim(),
            description: f.description.value.trim() || null,
            in_scope: inScope,
            out_of_scope: outScope,
          },
        });
        closeModal();
        toast("目标已创建");
        loadTargets();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  async function openTargetDetail(id) {
    try {
      const t = await api("/targets/" + id);
      openDrawer(`
        <h3>${esc(t.name)}</h3>
        <section>
          <h4>基本信息</h4>
          <dl class="kv">
            <dt>URL</dt><dd>${esc(t.url)}</dd>
            <dt>状态</dt><dd>${t.enabled ? "启用" : "停用"}</dd>
            <dt>描述</dt><dd>${esc(t.description || "—")}</dd>
            <dt>创建</dt><dd>${fmtDate(t.created_at)}</dd>
          </dl>
        </section>
        <section>
          <h4>授权范围</h4>
          <div>${t.in_scope.map((s) => `<span class="chip">${esc(s)}</span>`).join("") || "（空）"}</div>
        </section>
        <section>
          <h4>排除范围</h4>
          <div>${t.out_of_scope.map((s) => `<span class="chip">${esc(s)}</span>`).join("") || "（空）"}</div>
        </section>
        <div class="modal-actions">
          <button class="btn danger" data-del-target="${t.id}">删除</button>
          <button class="btn ghost" data-close>关闭</button>
        </div>`);

      $("[data-del-target]").addEventListener("click", async (e) => {
        if (!confirm("确认删除该目标？")) return;
        try {
          await api("/targets/" + id, { method: "DELETE" });
          closeDrawer();
          toast("目标已删除");
          loadTargets();
        } catch (err) {
          toast(err.message, true);
        }
      });
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---- 任务 ----
  async function loadTasks() {
    try {
      const list = await api("/tasks");
      const el = $("#task-list");
      if (!list.length) {
        el.innerHTML = `<div class="empty">暂无任务。点击右上角「新建任务」开始一次渗透测试。</div>`;
        return;
      }
      el.innerHTML = list
        .map(
          (t) => `
        <div class="card" data-task="${t.id}">
          <h3>${esc(t.name)}</h3>
          <div class="meta">
            <span class="badge st-${t.status}">${t.status}</span>
            ${t.cancel_requested ? '<span class="badge st-cancelled">取消中</span>' : ""}
          </div>
          <div class="desc">${esc(t.objective)}</div>
          <div class="meta" style="margin-top:6px"><span>更新 ${fmtDate(t.updated_at)}</span></div>
        </div>`
        )
        .join("");
      $$("[data-task]", el).forEach((c) =>
        c.addEventListener("click", () => openTaskDetail(c.dataset.task))
      );
    } catch (err) {
      toast(err.message, true);
    }
  }

  function openTaskModal() {
    openModal(`
      <h3>新建渗透任务</h3>
      <form id="task-form" class="form-grid">
        <label>选择授权目标
          <select name="target_id" required><option value="">加载中…</option></select>
        </label>
        <label>任务名称<input name="name" required placeholder="例如：外部资产侦察" /></label>
        <label>目标 (objective)
          <textarea name="objective" required placeholder="对目标执行被动侦察，识别暴露的服务与已知漏洞"></textarea>
        </label>
        <label>模型<input name="model" placeholder="留空使用默认" /></label>
        <label>最大轮数<input type="number" name="max_turns" value="50" min="1" max="200" /></label>
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-close>取消</button>
          <button type="submit" class="btn primary">创建并启动</button>
        </div>
      </form>`);

    // 填充目标下拉
    api("/targets")
      .then((targets) => {
        const sel = $("#task-form [name=target_id]");
        sel.innerHTML = targets
          .map((t) => `<option value="${t.id}">${esc(t.name)} (${esc(t.url)})</option>`)
          .join("");
      })
      .catch(() => {});

    $("#task-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const body = {
        target_id: f.target_id.value,
        name: f.name.value.trim(),
        objective: f.objective.value.trim(),
        max_turns: +f.max_turns.value,
      };
      if (f.model.value.trim()) body.model = f.model.value.trim();
      try {
        const task = await api("/tasks", { method: "POST", body });
        closeModal();
        toast("任务已创建并入队");
        openTaskDetail(task.id);
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  function statusBadge(s) {
    return `<span class="badge st-${s}">${s}</span>`;
  }

  async function openTaskDetail(id) {
    try {
      const [detail, events, findings, report] = await Promise.all([
        api("/tasks/" + id),
        api("/tasks/" + id + "/events?limit=50"),
        api("/tasks/" + id + "/findings"),
        api("/tasks/" + id + "/report").catch(() => null),
      ]);

      openDrawer(`
        <h3>${esc(detail.name)}</h3>
        <section>
          <h4>状态</h4>
          <div class="meta">
            ${statusBadge(detail.status)}
            ${detail.cancel_requested ? '<span class="badge st-cancelled">取消请求</span>' : ""}
          </div>
          <div class="modal-actions" style="margin-top:10px">
            ${
              ["queued", "running"].includes(detail.status)
                ? `<button class="btn danger" data-cancel="${id}">取消任务</button>`
                : ""
            }
            ${
              ["pending", "failed", "cancelled"].includes(detail.status)
                ? `<button class="btn" data-enqueue="${id}">重新入队</button>`
                : ""
            }
            <button class="btn" data-report="${id}">查看报告</button>
          </div>
        </section>

        <section>
          <h4>实时事件流</h4>
          <div id="live-stream" class="live-stream"></div>
        </section>

        <section>
          <h4>基本信息</h4>
          <dl class="kv">
            <dt>目标</dt><dd>${esc(detail.objective)}</dd>
            <dt>模型</dt><dd>${esc(detail.model || "默认")}</dd>
            <dt>尝试</dt><dd>${detail.attempts}</dd>
            <dt>操作员</dt><dd>${esc(detail.operator)}</dd>
            <dt>开始</dt><dd>${fmtDate(detail.started_at)}</dd>
            <dt>结束</dt><dd>${fmtDate(detail.finished_at)}</dd>
          </dl>
          ${detail.result ? `<div class="report-md" style="margin-top:10px">${esc(detail.result)}</div>` : ""}
          ${detail.error ? `<div class="report-md" style="margin-top:10px;color:var(--danger)">${esc(detail.error)}</div>` : ""}
        </section>

        <section>
          <h4>发现 (${findings.length})</h4>
          <div id="findings-box">
            ${
              findings.length
                ? findings
                    .map(
                      (f) => `
              <div class="finding">
                <div class="f-title">${esc(f.title)}
                  <span class="badge sev-${f.severity}">${f.severity}</span></div>
                <div class="f-desc">${esc(f.description || "")}</div>
                <div class="f-meta">
                  ${f.cwe ? `<span>${esc(f.cwe)}</span>` : ""}
                  ${f.confidence != null ? `<span>置信度 ${f.confidence}</span>` : ""}
                </div>
              </div>`
                    )
                    .join("")
                : '<div class="empty" style="padding:20px">暂无发现</div>'
            }
          </div>
        </section>

        <div class="modal-actions">
          <button class="btn ghost" data-close>关闭</button>
        </div>
      `);

      // 按钮事件
      const cancelBtn = $("[data-cancel]");
      if (cancelBtn)
        cancelBtn.addEventListener("click", async () => {
          try {
            await api("/tasks/" + id + "/cancel", { method: "POST" });
            toast("已发送取消请求");
            openTaskDetail(id);
          } catch (err) {
            toast(err.message, true);
          }
        });
      const enqBtn = $("[data-enqueue]");
      if (enqBtn)
        enqBtn.addEventListener("click", async () => {
          try {
            await api("/tasks/" + id + "/enqueue", { method: "POST" });
            toast("已重新入队");
            openTaskDetail(id);
          } catch (err) {
            toast(err.message, true);
          }
        });
      $("[data-report]").addEventListener("click", () => openReportDrawer(id));

      // 启动实时流
      startStream(id, $("#live-stream"), events);
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---- SSE 实时流 ----
  function closeStream() {
    if (state.es) {
      state.es.close();
      state.es = null;
    }
  }
  function startStream(taskId, box, initialEvents = []) {
    closeStream();
    const append = (ev) => {
      const div = document.createElement("div");
      div.className = "live-line";
      const type = ev.type || "event";
      let inner = "";
      if (type === "thought") {
        inner = `<span class="ev-think">思考 · ${esc(ev.text || JSON.stringify(ev.payload || ""))}</span>`;
      } else if (type === "tool_call_start" || type === "tool_call_end") {
        const p = ev.payload || {};
        const dir = type === "tool_call_start" ? "调用" : "返回";
        inner = `<span class="ev-tool">工具 ${dir} · ${esc(p.name || "")}</span> ${esc(
          p.args ? JSON.stringify(p.args) : ""
        )}`;
      } else if (type === "confidence") {
        inner = `<span class="ev-status">置信度 ${esc(ev.value ?? ev.payload ?? "")}</span>`;
      } else if (type === "task_status_changed") {
        inner = `<span class="ev-status">状态变更 → ${esc(ev.new_status || "")}</span>`;
      } else if (type === "agent_end" || type === "agent_error") {
        inner = `<span class="ev-status">${type === "agent_end" ? "运行完成" : "运行异常"}</span>`;
      } else if (type === "snapshot") {
        inner = `<span class="ev-status">已连接 · 初始状态：${esc(ev.status || "")}</span>`;
      } else {
        inner = `<span class="ev-type">${esc(type)}</span> ${esc(JSON.stringify(ev.payload || ""))}`;
      }
      div.innerHTML = inner;
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
    };

    (initialEvents || []).forEach(append);

    try {
      const es = new EventSource(`/api/v1/tasks/${taskId}/stream`, {
        withCredentials: true,
      });
      state.es = es;
      es.onmessage = (e) => {
        try {
          append(JSON.parse(e.data));
        } catch {}
      };
      es.addEventListener("ping", () => {});
      es.onerror = () => {
        // 浏览器会自动重连；终态由后端关闭
      };
    } catch (err) {
      box.innerHTML += `<div class="live-line">实时流不可用：${esc(err.message)}</div>`;
    }
  }

  // ---- 报告 ----
  async function openReportDrawer(id) {
    try {
      const [md, json] = await Promise.all([
        api("/tasks/" + id + "/report/markdown").then((r) => (typeof r === "string" ? r : "")),
        api("/tasks/" + id + "/report/json").then((r) => (typeof r === "string" ? r : "")),
      ]);
      openDrawer(`
        <h3>结构化报告</h3>
        <section>
          <div class="modal-actions" style="justify-content:flex-start">
            <button class="btn small" data-tab="md" data-active>Markdown</button>
            <button class="btn small" data-tab="json">JSON</button>
            <button class="btn small ghost" id="copy-report">复制</button>
          </div>
          <pre id="report-md" class="report-md">${esc(md)}</pre>
          <pre id="report-json" class="json-view hidden">${esc(json)}</pre>
        </section>
        <div class="modal-actions">
          <button class="btn ghost" data-close>关闭</button>
        </div>`);

      $("#report-md").parentElement.querySelectorAll("[data-tab]").forEach((b) => {
        b.addEventListener("click", () => {
          const isMd = b.dataset.tab === "md";
          $("#report-md").classList.toggle("hidden", !isMd);
          $("#report-json").classList.toggle("hidden", isMd);
        });
      });
      $("#copy-report").addEventListener("click", () => {
        const text = !$("#report-md").classList.contains("hidden")
          ? md
          : json;
        navigator.clipboard?.writeText(text).then(
          () => toast("已复制"),
          () => toast("复制失败", true)
        );
      });
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---- 审批 ----
  async function loadApprovals() {
    try {
      const list = await api("/approvals");
      const el = $("#approval-list");
      if (!list.length) {
        el.innerHTML = `<div class="empty">暂无审批请求。</div>`;
        return;
      }
      el.innerHTML = list
        .map(
          (a) => `
        <div class="card approval-card" data-approval="${a.id}">
          <div class="meta">
            <span class="badge ${a.status === "pending" ? "st-running" : a.status === "approved" ? "st-completed" : "st-cancelled"}">${a.status}</span>
            <span>${esc(a.tool_name)}</span>
          </div>
          <div class="a-tool" style="margin-top:6px">${esc(a.agent_name || "agent")}</div>
          <div class="a-args">${esc(JSON.stringify(a.tool_args, null, 2))}</div>
          ${a.decision_reason ? `<div class="f-desc">理由：${esc(a.decision_reason)}</div>` : ""}
          ${
            a.status === "pending"
              ? `<div class="a-actions">
                  <button class="btn primary" data-decide="${a.id}" data-d="approve">批准</button>
                  <button class="btn danger" data-decide="${a.id}" data-d="reject">拒绝</button>
                </div>`
              : ""
          }
        </div>`
        )
        .join("");
      $$("[data-decide]").forEach((b) =>
        b.addEventListener("click", () => decide(b.dataset.decide, b.dataset.d))
      );
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function decide(id, decision) {
    const reason = decision === "reject" ? prompt("拒绝理由（可选）：") : null;
    try {
      await api("/approvals/" + id + "/decision", {
        method: "POST",
        body: { decision, reason },
      });
      toast(decision === "approve" ? "已批准" : "已拒绝");
      loadApprovals();
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---- 审计 ----
  async function loadAudit() {
    try {
      const list = await api("/audit?limit=200");
      const el = $("#audit-list");
      if (!list.length) {
        el.innerHTML = `<div class="empty">暂无审计记录。</div>`;
        return;
      }
      el.innerHTML = `
        <table>
          <thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>结果</th><th>详情</th></tr></thead>
          <tbody>
            ${list
              .map(
                (a) => `
              <tr>
                <td>${fmtDate(a.created_at)}</td>
                <td>${esc(a.actor)}</td>
                <td>${esc(a.action)}</td>
                <td>${esc(a.outcome)}</td>
                <td>${esc(a.detail || (a.meta && JSON.stringify(a.meta)) || "")}</td>
              </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---- 初始化 ----
  function init() {
    bindAuth();
    bindNav();
    if (state.token) {
      enterApp();
    } else {
      showLogin();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
