/* Pobi v2 前端 SPA — 纯 vanilla JS，无构建步骤 */
(() => {
  "use strict";

  const API = "/api/v1";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ---- 状态 ----
  const state = {
    token: localStorage.getItem("pobi_token") || null,
    user: null,
    es: null, // EventSource
    workerTimer: null, // Worker 状态轮询定时器
  };

  // 从「新建任务」弹窗跳转过来时暂存的表单草稿
  let taskDraftAfterTarget = null;

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
  /**
   * 气泡时间戳：MM/DD HH:MM:SS。
   * - 接受 ISO 字符串或毫秒时间戳；缺省回退到当前时间，避免无时间戳的气泡。
   * - 跨天后会在日期段切换显示，年内不会重复年份，避免信息冗余。
   */
  function fmtClock(s) {
    const d = s == null ? new Date() : new Date(s);
    if (isNaN(d)) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(
      d.getMinutes()
    )}:${pad(d.getSeconds())}`;
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
  // 暂不开放注册
  // async function register(payload) {
  //   const data = await api("/auth/register", { method: "POST", body: payload });
  //   state.token = data.access_token;
  //   state.user = data.user;
  //   localStorage.setItem("pobi_token", state.token);
  // }
  async function loadMe() {
    state.user = await api("/auth/me");
  }

  // ---- 登录视图 ----
  function showLogin() {
    const lv = $("#login-view"), av = $("#app-view");
    if (lv) lv.classList.remove("hidden");
    if (av) av.classList.add("hidden");
  }
  function showApp() {
    const lv = $("#login-view"), av = $("#app-view");
    if (lv) lv.classList.add("hidden");
    if (av) av.classList.remove("hidden");
    const ui = $("#user-info");
    if (ui) {
      ui.textContent = state.user
        ? `${state.user.email} · ${state.user.is_admin ? "管理员" : "成员"}`
        : "";
    }
  }

  function bindAuth() {
    // 暂不开放注册：仅保留登录 tab，移除 register 相关引用
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        $$(".tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        const which = tab.dataset.tab;
        $("#login-form").classList.toggle("hidden", which !== "login");
      });
    });

    const loginForm = $("#login-form");
    if (loginForm) {
      loginForm.addEventListener("submit", async (e) => {
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
    }

    // 暂不开放注册
    // $("#register-form").addEventListener("submit", async (e) => {
    //   e.preventDefault();
    //   const f = e.target;
    //   $("#auth-error").textContent = "";
    //   try {
    //     await register({
    //       email: f.email.value.trim(),
    //       password: f.password.value,
    //       full_name: f.full_name.value.trim() || null,
    //       tenant_slug: f.tenant_slug.value.trim(),
    //     });
    //     await enterApp();
    //   } catch (err) {
    //     $("#auth-error").textContent = err.message;
    //   }
    // });

    const logoutBtn = $("#logout-btn");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", () => {
        closeStream();
        stopWorkerPoll();
        state.token = null;
        state.user = null;
        localStorage.removeItem("pobi_token");
        showLogin();
      });
    }
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
    await loadDashboard();
    startWorkerPoll();
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
        if (v === "tasks") loadDashboard();
        if (v === "targets") loadTargets();
        if (v === "approvals") loadApprovals();
        if (v === "audit") loadAudit();
        if (v === "tokens") loadTokens();
        if (v === "apikeys") loadApiKeys();
        if (v === "health") loadHealth();
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
      if (a === "refresh-tokens") loadTokens();
      if (a === "refresh-apikeys") loadApiKeys();
      if (a === "new-apikey") openApiKeyModal();
      if (a === "submit-apikey") createApiKey();
      if (a === "reveal-apikey") revealApiKey(act.dataset.id);
      if (a === "revoke-apikey") revokeApiKey(act.dataset.id);
      if (a === "reconcile") triggerReconcile();
      if (a === "refresh-health") loadHealth();
      if (a === "run-probe") runProbe();
      if (a === "cancel-modal") closeModal();
    });

    // 模态 / 抽屉关闭
    [$("#modal-root"), $("#drawer-root")].forEach((root) => {
      if (!root) return;
      root.addEventListener("click", (e) => {
        if (e.target.dataset.close !== undefined || e.target.closest("[data-close]")) {
          root.classList.add("hidden");
          closeStream();
        }
      });
    });

    // 价格配置表单提交
    const priceForm = $("#price-form");
    if (priceForm) {
      priceForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(priceForm);
        const payload = {
          price_input: Number(fd.get("price_input")) || 0,
          price_output: Number(fd.get("price_output")) || 0,
        };
        const btn = priceForm.querySelector('button[type="submit"]');
        btn.disabled = true;
        try {
          await api("/pricing", { method: "PUT", body: payload });
          toast("价格已保存", "ok");
          // 重新计算成本展示
          const summary = await api("/tasks/usage/summary");
          await applyPricingToUI(summary);
        } catch (err) {
          toast("保存失败: " + (err.message || err), "err");
        } finally {
          btn.disabled = false;
        }
      });
    }
  }

  // ---- Token 用量页 ----
  function fmtNum(n) {
    return Number(n || 0).toLocaleString("en-US");
  }
  function fmtCost(value, currency) {
    if (!value || value <= 0) return "—";
    return (
      (value).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      }) +
      " " +
      (currency || "USD")
    );
  }
  function estimateCost(promptTokens, completionTokens, price) {
    const p = (Number(promptTokens) || 0) * (Number(price.price_input) || 0) / 1e6;
    const c = (Number(completionTokens) || 0) * (Number(price.price_output) || 0) / 1e6;
    return p + c;
  }
  async function loadTokens() {
    const sumEl = $("#sum-total");
    if (sumEl) sumEl.textContent = "…";
    try {
      const [summary, pricing] = await Promise.all([
        api("/tasks/usage/summary"),
        api("/pricing"),
      ]);
      renderTokenSummary(summary, pricing);
      renderTokenTasks(pricing);
    } catch (err) {
      toast("加载 Token 用量失败: " + (err.message || err), "err");
    }
  }
  async function applyPricingToUI(summary) {
    const pricing = await api("/pricing");
    renderTokenSummary(summary, pricing);
  }
  function renderTokenSummary(summary, pricing) {
    const p = Number(summary.total_prompt_tokens) || 0;
    const c = Number(summary.total_completion_tokens) || 0;
    const t = Number(summary.total_tokens) || 0;
    const cost = estimateCost(p, c, pricing);
    $("#sum-prompt").textContent = fmtNum(p);
    $("#sum-completion").textContent = fmtNum(c);
    $("#sum-total").textContent = fmtNum(t);
    $("#sum-tasks").textContent = fmtNum(summary.task_count);
    $("#sum-prompt-cost").textContent = fmtCost(
      (p * (Number(pricing.price_input) || 0)) / 1e6, pricing.currency
    );
    $("#sum-completion-cost").textContent = fmtCost(
      (c * (Number(pricing.price_output) || 0)) / 1e6, pricing.currency
    );
    $("#sum-total-cost").textContent = fmtCost(cost, pricing.currency);
    $("#sum-task-cost").textContent = fmtCost(cost, pricing.currency);
    // 回填价格表单
    const form = $("#price-form");
    if (form) {
      form.price_input.value = pricing.price_input || 0;
      form.price_output.value = pricing.price_output || 0;
    }
    $("#price-cur-input").textContent = pricing.currency || "USD";
    $("#price-cur-output").textContent = pricing.currency || "USD";
  }
  async function renderTokenTasks(pricing) {
    let tasks;
    try {
      tasks = await api("/tasks");
    } catch {
      tasks = [];
    }
    const tbody = $("#token-task-table tbody");
    const empty = $("#token-empty");
    if (!tbody) return;
    if (!tasks.length) {
      tbody.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    tbody.innerHTML = tasks
      .map((tk) => {
        const p = Number(tk.prompt_tokens) || 0;
        const c = Number(tk.completion_tokens) || 0;
        const t = Number(tk.total_tokens) || 0;
        const cost = estimateCost(p, c, pricing);
        return `<tr data-task-id="${tk.id}">
          <td class="col-status"><span class="st ${tk.status}">${tk.status}</span></td>
          <td class="cell-name" title="${(tk.name || "").replace(/"/g, "&quot;")}">${tk.name || "—"}</td>
          <td class="col-tokens">${fmtNum(p)}</td>
          <td class="col-tokens">${fmtNum(c)}</td>
          <td class="col-tokens"><b>${fmtNum(t)}</b></td>
          <td class="col-tokens cost">${fmtCost(cost, pricing.currency)}</td>
          <td class="col-action"><button class="btn ghost small" data-open-task="${tk.id}">详情</button></td>
        </tr>`;
      })
      .join("");
  }

  // ---- API 令牌（PAT）管理 ----
  function fmtDateTime(v) {
    if (!v) return "—";
    const d = new Date(v);
    return isNaN(d) ? "—" : d.toLocaleString();
  }

  async function loadApiKeys() {
    const tbody = $("#apikey-table tbody");
    const empty = $("#apikey-empty");
    tbody.innerHTML = `<tr><td colspan="7" class="muted">加载中…</td></tr>`;
    try {
      const list = await api("/tokens");
      if (!Array.isArray(list) || list.length === 0) {
        tbody.innerHTML = "";
        empty.classList.remove("hidden");
        return;
      }
      empty.classList.add("hidden");
      tbody.innerHTML = list
        .map((k) => {
          const scopes = (k.scopes || []).join(", ") || "*";
          const expired = k.expires_at && new Date(k.expires_at) < new Date();
          const statusCls = k.revoked ? "revoked" : expired ? "expired" : "active";
          const statusTxt = k.revoked ? "已吊销" : expired ? "已过期" : "有效";
          return `<tr data-id="${k.id}">
            <td><span class="token-name">${escapeHtml(k.name)}</span></td>
            <td><span class="token-key">${escapeHtml(k.prefix)}...</span></td>
            <td><span class="token-scope">${escapeHtml(scopes)}</span></td>
            <td><span class="token-expiry">${fmtDateTime(k.expires_at) || "长期"}</span></td>
            <td><span class="token-expiry">${fmtDateTime(k.last_used_at) || "—"}</span></td>
            <td><span class="token-status ${statusCls}"><i class="status-dot"></i>${statusTxt}</span></td>
            <td class="col-action">
              <button class="btn ghost xs" data-action="reveal-apikey" data-id="${k.id}">查看</button>
              <button class="btn danger xs" data-action="revoke-apikey" data-id="${k.id}" ${k.revoked ? "disabled style=opacity:.4;cursor:not-allowed" : ""}>吊销</button>
            </td>
          </tr>`;
        })
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">加载失败：${escapeHtml(e.message || e)}</td></tr>`;
    }
  }

  function openApiKeyModal() {
    openModal(`
      <h3 style="margin-bottom:4px">新建 API 令牌</h3>
      <p class="muted" style="margin-bottom:16px;font-size:12px">生成后请立即保存明文令牌，仅显示一次。</p>
      <div class="apikey-form">
        <div class="form-row">
          <label>名称</label>
          <input id="ak-name" type="text" placeholder="例如：benchmark-script" />
        </div>
        <div class="form-hint">用于标识用途，如 ci-cd-deploy、benchmark-script</div>
        <div class="form-row">
          <label>有效期</label>
          <input id="ak-expire" type="number" min="1" max="3650" placeholder="留空 = 长期有效" />
          <span style="font-size:12px;color:var(--text-muted)">天</span>
        </div>
        <div class="form-row">
          <label>权限范围</label>
          <input id="ak-scopes" type="text" placeholder="task:write, target:read" />
        </div>
        <div class="form-hint">逗号分隔，留空表示全量权限（*）</div>
      </div>
      <div class="modal-actions">
        <button class="btn ghost" data-action="cancel-modal">取消</button>
        <button class="btn primary" data-action="submit-apikey">生成令牌</button>
      </div>
    `);
  }

  async function createApiKey() {
    const name = ($("#ak-name").value || "").trim();
    if (!name) {
      alert("请填写名称");
      return;
    }
    const expireRaw = ($("#ak-expire").value || "").trim();
    const scopesRaw = ($("#ak-scopes").value || "").trim();
    const body = {
      name,
      expires_in_days: expireRaw ? Number(expireRaw) : null,
      scopes: scopesRaw ? scopesRaw.split(",").map((s) => s.trim()).filter(Boolean) : [],
    };
    try {
      const created = await api("/tokens", { method: "POST", body });
      openModal(`
        <h3 style="margin-bottom:4px">令牌已生成</h3>
        <p class="muted" style="margin-bottom:12px;font-size:12px">
          请立即复制保存，明文仅显示这一次${created.revealable ? "（之后可再次查看）" : "（未配置加密密钥，之后无法查看）"}。
        </p>
        <div class="result-box">
          <pre id="ak-plain">${escapeHtml(created.plaintext_token)}</pre>
          <div class="copy-row">
            <span class="muted" style="font-size:11px">点击复制到剪贴板</span>
            <button class="btn primary xs" id="ak-copy">复制令牌</button>
        </div>
      `);
      $("#ak-copy").onclick = () => {
        const t = $("#ak-plain").textContent;
        navigator.clipboard?.writeText(t);
        $("#ak-copy").textContent = "已复制";
      };
      loadApiKeys();
    } catch (e) {
      alert("创建失败：" + (e.message || e));
    }
  }

  async function revealApiKey(id) {
    try {
      const r = await api(`/tokens/${id}/reveal`, { method: "POST", body: {} });
      openModal(`
        <h3 style="margin-bottom:4px">令牌明文</h3>
        <p class="muted" style="margin-bottom:12px;font-size:12px">请妥善保管，勿泄露给他人。</p>
        <div class="result-box">
          <pre id="ak-plain">${escapeHtml(r.plaintext_token)}</pre>
          <div class="copy-row">
            <span class="muted" style="font-size:11px">点击复制到剪贴板</span>
            <button class="btn primary xs" id="ak-copy">复制令牌</button>
          </div>
        </div>
      `);
      $("#ak-copy").onclick = () => {
        const t = $("#ak-plain").textContent;
        navigator.clipboard?.writeText(t);
        $("#ak-copy").textContent = "已复制";
      };
    } catch (e) {
      alert("查看失败：" + (e.message || e));
    }
  }

  async function revokeApiKey(id) {
    if (!confirm("确认吊销该令牌？吊销后使用该令牌的脚本将无法调用 API。")) return;
    try {
      await api(`/tokens/${id}`, { method: "DELETE" });
      loadApiKeys();
    } catch (e) {
      alert("吊销失败：" + (e.message || e));
    }
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
        <div class="form-divider">验证策略（怎样才算找到漏洞）</div>
        <label>FLAG 正则
          <input name="flag_regex" placeholder="例如 FLAG\\{[^}]+\\}，留空则仅用 LLM 判定" />
        </label>
        <label>FLAG 格式
          <input name="validation_format" placeholder="例如 FLAG{}" />
        </label>
        <label class="has-hint">
          <span class="field-label">
            信心阈值带 (0–1)
            <span class="info-icon" data-hint-target="hint-confidence" title="查看说明">ⓘ</span>
          </span>
          <input name="confidence_threshold" type="number" min="0" max="1" step="0.1" value="0.6" />
          <span class="hint-box hidden" id="hint-confidence">LLM 判定一个发现是否成立所需的最低置信度。值越高，误报越少，但也可能漏掉边界案例。</span>
        </label>
        <label class="has-hint">
          <span class="field-label">
            任务树最大深度
            <span class="info-icon" data-hint-target="hint-max-depth" title="查看说明">ⓘ</span>
          </span>
          <input name="max_tree_depth" type="number" min="1" max="16" step="1" value="4" />
          <span class="hint-box hidden" id="hint-max-depth">任务分解与子任务嵌套的最大层数。深度越大，Agent 探索越深入，但耗时和 Token 消耗也会显著增加。</span>
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

    // 提示图标点击展开/收起说明
    $$(".info-icon", $("#target-form")).forEach((icon) => {
      icon.addEventListener("click", () => {
        const box = $(`#${icon.dataset.hintTarget}`);
        if (box) box.classList.toggle("hidden");
      });
    });

    $("#target-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const ct = parseFloat(f.confidence_threshold.value);
      const mtd = parseInt(f.max_tree_depth.value, 10);
      try {
        const newTarget = await api("/targets", {
          method: "POST",
          body: {
            name: f.name.value.trim(),
            url: f.url.value.trim(),
            description: f.description.value.trim() || null,
            in_scope: inScope,
            out_of_scope: outScope,
            flag_regex: f.flag_regex.value.trim() || null,
            validation_format: f.validation_format.value.trim() || null,
            confidence_threshold: Number.isFinite(ct) ? ct : 0.6,
            max_tree_depth: Number.isFinite(mtd) ? mtd : 4,
          },
        });
        closeModal();
        toast("目标已创建");
        loadTargets();
        if (taskDraftAfterTarget) {
          const draft = taskDraftAfterTarget;
          taskDraftAfterTarget = null;
          openTaskModal(draft, newTarget.id);
        }
      } catch (err) {
        toast(err.message, true);
      }
    });

    // 若从任务弹窗跳转而来，点击取消时恢复任务弹窗
    const cancelBtn = $("#target-form [data-close]");
    if (cancelBtn && taskDraftAfterTarget) {
      cancelBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const draft = taskDraftAfterTarget;
        taskDraftAfterTarget = null;
        openTaskModal(draft);
      });
    }
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
        <section>
          <h4>验证策略</h4>
          <dl class="kv">
            <dt>FLAG 正则</dt><dd>${esc(t.flag_regex || "—")}</dd>
            <dt>FLAG 格式</dt><dd>${esc(t.validation_format || "—")}</dd>
            <dt>信心阈值带</dt><dd>${t.confidence_threshold}</dd>
            <dt>任务树深度</dt><dd>${t.max_tree_depth}</dd>
          </dl>
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

  // ---- 任务看板 ----
  async function loadDashboard() {
    try {
      const [tasks, approvals] = await Promise.all([
        api("/tasks"),
        api("/approvals").catch(() => []),
      ]);
      renderStats(tasks, approvals);
      renderActiveTasks(tasks);
      renderRecentCompleted(tasks);
      renderHistoryTasks(tasks);
    } catch (err) {
      toast(err.message, true);
    }
  }

  // 手动对账：不再于每次加载看板时自动触发（避免误杀正在运行的健康任务）。
  // 仅由用户点击“对账”按钮显式调用，作为运维自愈手段。
  async function triggerReconcile() {
    const btn = $("#reconcile-btn");
    if (btn) {
      btn.disabled = true;
      btn.dataset.busy = "1";
    }
    try {
      const r = await api("/system/task-reconcile", { method: "POST" });
      if (r && r.ok) {
        if (r.terminated_count > 0) {
          const byId = Object.fromEntries((r.terminated || []).map((t) => [t.task_id, t]));
          const tasks = await api("/tasks").catch(() => []);
          const approvals = await api("/approvals").catch(() => []);
          for (const task of tasks) {
            const t = byId[task.id];
            if (t) {
              task.status = t.new_status;
              if (!task.error) task.error = t.reason;
            }
          }
          renderStats(tasks, approvals);
          renderActiveTasks(tasks);
          renderRecentCompleted(tasks);
          renderHistoryTasks(tasks);
          toast(`已终止 ${r.terminated_count} 个异常任务`);
        } else {
          toast("对账完成，未发现需要终止的异常任务");
        }
      } else {
        toast((r && r.error) || "对账失败", true);
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        delete btn.dataset.busy;
      }
    }
  }

  // ---- Worker 状态（ARQ 任务消费进程在线探测）----
  async function loadWorkerStatus() {
    const pill = $("#worker-status");
    if (!pill) return;
    const dot = pill.querySelector(".status-dot");
    const text = pill.querySelector(".worker-text");
    try {
      const s = await api("/system/worker-status");
      if (!s.available) {
        pill.dataset.state = "offline";
        if (dot) {}
        text.textContent = "Worker 不可用";
        pill.title = "无法连接 Redis，Worker 状态未知";
        return;
      }
      if (s.online) {
        pill.dataset.state = "online";
        const q = s.queue_depth || 0;
        const st = s.stats || {};
        const ongoing = st.j_ongoing || 0;
        text.textContent = `Worker 在线 · 队列 ${q} 个`;
        pill.title = `任务消费 Worker 在线（进行中 ${ongoing} · 完成 ${st.j_complete || 0} · 失败 ${st.j_failed || 0}）`;
      } else {
        pill.dataset.state = "offline";
        text.textContent = "Worker 离线";
        pill.title = "未检测到运行中的任务消费 Worker（任务将不会被执行）";
      }
    } catch {
      pill.dataset.state = "unknown";
      text.textContent = "检测中…";
    }
  }

  function startWorkerPoll() {
    stopWorkerPoll();
    loadWorkerStatus();
    state.workerTimer = setInterval(loadWorkerStatus, 10000);
  }
  function stopWorkerPoll() {
    if (state.workerTimer) {
      clearInterval(state.workerTimer);
      state.workerTimer = null;
    }
  }

  function renderStats(tasks, approvals) {
    const counts = {
      running: tasks.filter((t) => t.status === "running").length,
      queued: tasks.filter((t) => t.status === "queued").length,
      pending: approvals.filter((a) => a.status === "pending").length,
      completed: tasks.filter((t) => t.status === "completed").length,
    };
    animateNumber($("#stat-running"), counts.running);
    animateNumber($("#stat-queued"), counts.queued);
    animateNumber($("#stat-pending"), counts.pending);
    animateNumber($("#stat-completed"), counts.completed);
  }

  function animateNumber(el, target) {
    if (!el) return;
    const start = parseInt(el.textContent, 10) || 0;
    if (start === target) return;
    const duration = 500;
    const startTime = performance.now();
    function step(now) {
      const p = Math.min(1, (now - startTime) / duration);
      el.textContent = Math.round(start + (target - start) * p);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function statusText(s) {
    const map = {
      queued: "排队中",
      pending: "待审批",
      running: "运行中",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
    };
    return map[s] || s;
  }

  function taskProgress(t) {
    if (t.status === "completed") return 100;
    if (t.status === "queued") return 10;
    if (t.status === "pending") return 35;
    if (t.status === "running") return Math.min(90, 45 + Math.min(45, (t.attempts || 0) * 1));
    return 0;
  }

  const iconTarget = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>`;
  const iconConstraint = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;
  const iconClock = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>`;

  function renderActiveTasks(tasks) {
    const active = tasks.filter((t) => ["running", "queued", "pending"].includes(t.status));
    const countEl = $("#active-count");
    if (countEl) countEl.textContent = active.length;
    const el = $("#active-task-list");
    if (!el) return;
    if (!active.length) {
      el.innerHTML = `<div class="empty">暂无活跃任务</div>`;
      return;
    }
    el.innerHTML = active
      .map((t) => {
        // 本地超时兜底：running 且 started_at 距今超过 6h，提示“已超时”（后端对账会自愈为 failed）
        const timedOut =
          t.status === "running" &&
          t.started_at &&
          Date.now() - new Date(t.started_at).getTime() > 6 * 3600 * 1000;
        const timeoutBadge = timedOut
          ? `<span class="badge st-failed" title="运行已超 6 小时，后端将对账终止" style="margin-left:6px">已超时</span>`
          : "";
        return `
        <div class="active-task-card" data-task="${t.id}" data-status="${t.status}">
          <div class="active-task-header">
            <div class="active-task-title">
              <span class="status-dot st-${t.status}"></span>
              <span>${esc(t.name)}</span>
            </div>
            <span class="badge st-${t.status}">${statusText(t.status)}</span>${timeoutBadge}
          </div>
          <div class="active-task-target">${iconTarget}${esc(t.objective || "—")}</div>
          <div class="active-task-constraint">${iconConstraint}${esc(t.agent_mode || "hacker")}</div>
          <div class="active-task-progress">
            <div class="progress-track">
              <div class="progress-fill" style="width:${taskProgress(t)}%"></div>
            </div>
          </div>
          <div class="active-task-footer">
            <div class="active-task-time">${iconClock}${fmtDate(t.created_at)}</div>
            <div class="active-task-action">打开控制台 →</div>
          </div>
        </div>`;
      })
      .join("");
    $$("[data-task]", el).forEach((c) =>
      c.addEventListener("click", () => openTaskDetail(c.dataset.task))
    );
  }

  function renderRecentCompleted(tasks) {
    const completed = tasks
      .filter((t) => t.status === "completed")
      .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
      .slice(0, 5);
    const el = $("#recent-completed-list");
    if (!el) return;
    if (!completed.length) {
      el.innerHTML = `<div class="empty" style="padding:28px">暂无完成记录</div>`;
      return;
    }
    el.innerHTML = completed
      .map(
        (t) => `
        <div class="recent-item" data-task="${t.id}">
          <div class="recent-title">${esc(t.name)}</div>
          <div class="recent-meta">
            <span>${fmtDate(t.created_at)}</span>
            <span class="badge st-completed">已完成</span>
          </div>
        </div>`
      )
      .join("");
    $$("[data-task]", el).forEach((c) =>
      c.addEventListener("click", () => openTaskDetail(c.dataset.task))
    );
  }

  function renderHistoryTasks(tasks) {
    const inactive = tasks
      .filter((t) => !["running", "queued", "pending"].includes(t.status))
      .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
    const table = $("#history-task-table");
    const empty = $("#history-empty");
    if (!table) return;
    const tbody = table.querySelector("tbody");
    if (!inactive.length) {
      table.classList.add("hidden");
      if (empty) empty.classList.remove("hidden");
      return;
    }
    table.classList.remove("hidden");
    if (empty) empty.classList.add("hidden");
    tbody.innerHTML = inactive
      .map(
        (t) => `
        <tr data-task="${t.id}">
          <td data-label="状态" class="col-status"><span class="badge st-${t.status}">${statusText(t.status)}</span></td>
          <td data-label="任务" class="task-name-cell">${esc(t.name)}</td>
          <td data-label="目标" class="task-target-cell">${esc(t.objective || "—")}</td>
          <td data-label="约束" class="task-mode-cell">${esc(t.agent_mode || "hacker")}</td>
          <td data-label="创建时间" class="task-time-cell">${fmtDate(t.created_at)}</td>
          <td data-label="操作" class="task-action-cell"><button class="btn-ghost" data-open-task="${t.id}">查看</button></td>
        </tr>`
      )
      .join("");
    $$("tr[data-task]", tbody).forEach((r) =>
      r.addEventListener("click", () => openTaskDetail(r.dataset.task))
    );
    $$("[data-open-task]", tbody).forEach((b) =>
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        openTaskDetail(b.dataset.openTask);
      })
    );
  }

  function openTaskModal(draft = null, preselectedTargetId = null) {
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
        <label>执行模式
          <select name="agent_mode">
            <option value="hacker" selected>受控模式（hacker，高危调用需审批）</option>
            <option value="yolo">主动验证（yolo，自动批准高危调用）</option>
          </select>
        </label>
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-close>取消</button>
          <button type="submit" class="btn primary">创建并启动</button>
        </div>
      </form>`);

    const form = $("#task-form");
    if (draft) {
      form.name.value = draft.name || "";
      form.objective.value = draft.objective || "";
      form.model.value = draft.model || "";
      form.max_turns.value = draft.max_turns || 50;
      form.agent_mode.value = draft.agent_mode || "hacker";
    }

    // 填充目标下拉
    api("/targets")
      .then((targets) => {
        const sel = form.target_id;
        sel.innerHTML =
          targets
            .map((t) => `<option value="${t.id}">${esc(t.name)} (${esc(t.url)})</option>`)
            .join("") +
          `<option value="new-target">+ 新建授权目标</option>`;
        if (preselectedTargetId) sel.value = preselectedTargetId;
        else if (draft && draft.target_id) sel.value = draft.target_id;
      })
      .catch(() => {});

    // 选择「新建授权目标」时暂存草稿并跳转
    form.target_id.addEventListener("change", (e) => {
      if (e.target.value === "new-target") {
        taskDraftAfterTarget = {
          target_id: "",
          name: form.name.value,
          objective: form.objective.value,
          model: form.model.value,
          max_turns: form.max_turns.value,
          agent_mode: form.agent_mode.value,
        };
        closeModal();
        openTargetModal();
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const body = {
        target_id: f.target_id.value,
        name: f.name.value.trim(),
        objective: f.objective.value.trim(),
        max_turns: +f.max_turns.value,
        agent_mode: f.agent_mode.value,
      };
      if (f.model.value.trim()) body.model = f.model.value.trim();
      try {
        const task = await api("/tasks", { method: "POST", body });
        closeModal();
        // Worker 离线时任务不会被执行，给出提示但不阻断创建
        const pill = $("#worker-status");
        if (pill && pill.dataset.state === "offline") {
          toast("任务已创建并入队，但 Worker 离线，任务暂不会执行", true);
        } else {
          toast("任务已创建并入队");
        }
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
    return openTaskConsole(id);
  }

  // ---- 渗透任务实时监控控制台（全屏三栏） ----
  async function openTaskConsole(id) {
    try {
      const [detail, live, plan, findings, usage] = await Promise.all([
        api("/tasks/" + id),
        api("/tasks/" + id + "/live"),
        api("/tasks/" + id + "/plan"),
        api("/tasks/" + id + "/findings"),
        api("/tasks/" + id + "/usage").catch(() => null),
      ]);

      state.consoleId = id;
      const mode = detail.agent_mode || "hacker";
      const modeLabel = mode === "yolo" ? "主动验证" : "受控模式";
      const canControl = ["queued", "running"].includes(detail.status);

      const up = Number(usage?.prompt_tokens) || 0;
      const uc = Number(usage?.completion_tokens) || 0;
      const ut = Number(usage?.total_tokens) || 0;

      $("#console-body").innerHTML = `
        <div class="console-shell">
          <!-- 任务头 -->
          <header class="console-head">
            <button class="console-back" data-close-console title="返回看板">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
            </button>
            <div class="console-head-main">
              <div class="console-title-row">
                <span class="task-url" title="${esc(detail.objective || "")}">${esc(detail.name || detail.objective || "")}</span>
                ${statusBadge(detail.status)}
                <span class="mode-tag mode-${mode}">${modeLabel}</span>
                ${
                  live.pending_instructions
                    ? `<span class="mode-tag pending">待生效指令 ${live.pending_instructions}</span>`
                    : ""
                }
              </div>
              <div class="console-sub">主控正在执行 · ${esc(live.current_phase || "初始化")} · 当前智能体 ${esc(
        live.current_agent || "—"
      )}</div>
            </div>
            <div class="console-head-actions">
              ${
                canControl
                  ? `<button class="btn danger small" data-console-cancel>中断</button>`
                  : ""
              }
              ${
                ["pending", "failed", "cancelled"].includes(detail.status)
                  ? `<button class="btn small" data-console-enqueue>继续</button>`
                  : ""
              }
              <button class="btn small" data-console-report>报告</button>
            </div>
          </header>

          ${
            ut > 0
              ? `<div class="console-tokens">
                  <div class="ctok ctok-send"><span class="ctok-num">${fmtNum(up)}</span><span class="ctok-lab">发送</span></div>
                  <div class="ctok ctok-recv"><span class="ctok-num">${fmtNum(uc)}</span><span class="ctok-lab">接收</span></div>
                  <div class="ctok ctok-total"><span class="ctok-num">${fmtNum(ut)}</span><span class="ctok-lab">总 Token</span></div>
                </div>`
              : ""
          }

          <div class="console-grid">
            <!-- 左栏：执行计划 / 运行视图 -->
            <aside class="console-col col-left">
              <div class="card plan-card">
                <div class="card-head">
                  <h4>执行计划</h4>
                  <span class="plan-progress" id="plan-progress">${plan.completed}/${plan.total}</span>
                </div>
                <div class="plan-bar"><div class="plan-bar-fill" id="plan-bar-fill" style="width:${
                  plan.total ? Math.round((plan.completed / plan.total) * 100) : 0
                }%"></div></div>
                <div id="plan-list" class="plan-list">
                  ${
                    plan.steps.length
                      ? plan.steps
                          .map(
                            (s) => `
                    <div class="plan-step st-${s.status}">
                      <span class="plan-dot"></span>
                      <span class="plan-seq">${s.seq + 1}</span>
                      <span class="plan-title">${esc(s.title)}</span>
                      <span class="plan-state">${planStateLabel(s.status)}</span>
                    </div>`
                          )
                          .join("")
                      : '<div class="empty" style="padding:14px">暂未生成计划</div>'
                  }
                </div>
              </div>

              <div class="card agent-card">
                <div class="card-head"><h4>运行视图</h4><span class="agent-count" id="agent-count">${
                  live.agents.length
                }</span></div>
                <div id="agent-list" class="agent-list">
                  ${
                    live.agents.length
                      ? live.agents
                          .map(
                            (a) => `
                    <div class="agent-row st-${a.status}">
                      <span class="agent-dot"></span>
                      <span class="agent-name">${esc(a.name)}</span>
                      <span class="agent-role">${esc(a.role || "")}</span>
                      <span class="agent-state">${agentStateLabel(a.status)}</span>
                    </div>`
                          )
                          .join("")
                      : '<div class="empty" style="padding:14px">暂无智能体</div>'
                  }
                </div>
              </div>
            </aside>

            <!-- 中栏：主控对话 -->
            <section class="console-col col-center">
              <div class="console-phase">
                <span class="phase-label">当前阶段</span>
                <span class="phase-value" id="phase-value">${esc(live.current_phase || "初始化")}</span>
                <span class="phase-agent" id="phase-agent">${esc(live.current_agent || "—")}</span>
              </div>
              <div id="chat-stream" class="chat-stream"></div>
              <div class="console-input">
                <textarea id="instr-input" class="instr-input" rows="2" placeholder="给主控发送新的指令或追问…"></textarea>
                <button class="btn primary" id="instr-send" ${
                  canControl ? "" : "disabled"
                }>发送</button>
              </div>
              ${
                canControl
                  ? ""
                  : '<div class="console-input-hint">任务非运行中，指令将在继续后生效</div>'
              }
            </section>

            <!-- 右栏：攻击范围 / 审批 -->
            <aside class="console-col col-right">
              <div class="card scope-card">
                <div class="card-head"><h4>攻击范围</h4></div>
                <div class="scope-url">${esc(live.target_url || detail.objective || "—")}</div>
                <div class="scope-tags" id="scope-tags">
                  <span class="chip">${modeLabel}</span>
                  <span class="chip">自动验证</span>
                </div>
              </div>

              <div class="card approval-card">
                <div class="card-head">
                  <h4>审批</h4>
                  <button class="btn ghost tiny" data-console-refresh-approval>刷新</button>
                </div>
                <div id="approval-list-console" class="approval-list-console">
                  <div class="empty" style="padding:14px">加载中…</div>
                </div>
              </div>
            </aside>
          </div>
        </div>
      `;

      $("#console-root").classList.remove("hidden");
      document.body.style.overflow = "hidden";

      // 关闭
      $("[data-close-console]").addEventListener("click", closeConsole);
      const cancelBtn = $("[data-console-cancel]");
      if (cancelBtn)
        cancelBtn.addEventListener("click", async () => {
          try {
            await api("/tasks/" + id + "/cancel", { method: "POST" });
            toast("已发送中断请求");
            openTaskConsole(id);
          } catch (e) {
            toast(e.message, true);
          }
        });
      const enqBtn = $("[data-console-enqueue]");
      if (enqBtn)
        enqBtn.addEventListener("click", async () => {
          try {
            await api("/tasks/" + id + "/enqueue", { method: "POST" });
            toast("已重新入队");
            openTaskConsole(id);
          } catch (e) {
            toast(e.message, true);
          }
        });
      $("[data-console-report]").addEventListener("click", () => openReportDrawer(id));
      $("[data-console-refresh-approval]").addEventListener("click", () => loadConsoleApprovals(id));

      // 指令发送（真正干预运行）
      const sendBtn = $("#instr-send");
      const sendInstr = async () => {
        const ta = $("#instr-input");
        const text = ta.value.trim();
        if (!text) return;
        appendChat({ role: "user", text });
        ta.value = "";
        try {
          await api("/tasks/" + id + "/instructions", {
            method: "POST",
            body: JSON.stringify({ instruction: text }),
          });
          appendChat({ role: "system", text: "指令已下发，将在下一阶段生效" });
        } catch (e) {
          appendChat({ role: "system", text: "指令下发失败：" + e.message, err: true });
        }
      };
      sendBtn.addEventListener("click", sendInstr);
      $("#instr-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendInstr();
        }
      });

      // 渲染初始对话流 & 审批
      renderChatFromEvents(live.recent_events || []);
      await loadConsoleApprovals(id);

      // 启动实时流（SSE 联动计划/对话/审批）
      startConsoleStream(id);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function closeConsole() {
    closeStream();
    state.consoleId = null;
    $("#console-root").classList.add("hidden");
    document.body.style.overflow = "";
  }

  function planStateLabel(s) {
    return { pending: "待执行", running: "进行中", completed: "已完成", failed: "失败" }[s] || s;
  }
  function agentStateLabel(s) {
    return { idle: "空闲", running: "运行中", done: "完成", error: "异常" }[s] || s;
  }

  // 把后端事件渲染为对话气泡
  function renderChatFromEvents(events) {
    const box = $("#chat-stream");
    if (!box) return;
    box.innerHTML = "";
    (events || []).forEach((ev) => appendChat(eventToChat(ev)));
  }

  function eventToChat(ev) {
    const type = ev.type || ev.event_type || "event";
    const p = ev.payload || {};
    // 事件时间戳：优先用后端持久化字段 created_at；其次兼容 timestamp / at；最终由 appendChat 兜底为前端当前时间
    const ts = ev.created_at || ev.timestamp || ev.at;
    // 统一给气泡对象追加 created_at，避免每个分支手工塞字段
    const withTs = (o) => (o ? { ...o, created_at: o.created_at || ts } : o);
    if (type === "thought")
      return withTs({ role: "ai", kind: "thought", text: p.text || p.content || JSON.stringify(p) });
    if (type === "tool_call_start")
      return withTs({ role: "ai", kind: "tool", text: `工具调用 · ${p.name || ""}` + (p.args ? "\n" + JSON.stringify(p.args) : "") });
    if (type === "tool_call_end")
      return withTs({ role: "ai", kind: "tool", text: `工具返回 · ${p.name || ""}` + (p.result ? "\n" + JSON.stringify(p.result).slice(0, 400) : "") });
    if (type === "confidence")
      return withTs({ role: "ai", kind: "status", text: `置信度 ${p.value ?? p.confidence ?? ""}` });
    if (type === "task_status_changed")
      return withTs({ role: "ai", kind: "status", text: `状态变更 → ${p.new_status || ""}` });
    if (type === "validation")
      return withTs({ role: "ai", kind: "validation", text: p.message || p.detail || "验证结果" });
    if (type === "result")
      return withTs({ role: "ai", kind: "result", text: p.content || p.summary || "阶段性结果" });
    if (type === "report_task_event")
      return withTs({ role: "ai", kind: "report", title: p.title || "安全评估报告", text: p.content || p.summary || "报告生成" });
    if (type === "agent_start")
      return withTs({ role: "ai", kind: "status", text: `智能体启动 · ${p.agent_name || ""}` });
    if (type === "agent_end")
      return withTs({ role: "ai", kind: "status", text: `智能体完成 · ${p.agent_name || ""}` });
    if (type === "phase_changed")
      return withTs({ role: "ai", kind: "phase", text: `阶段流转 → ${p.new_phase || ""}` });
    // ── LLM 可观测性：迭代 / 输入 / 响应（Worker 终端有但前端缺失的关键信息）──
    if (type === "llm_iteration")
      return withTs({ role: "ai", kind: "llm-iter", text: `Iteration ${p.iteration ?? "?"} · ${p.message_count ?? 0} messages`, agentName: p.agent_name || "" });
    if (type === "llm_input") {
      const label = p.role === "tool"
        ? `LLM Input · Tool Result (${p.tool_name || "?"})`
        : `LLM Input · ${p.role || "user"}`;
      return withTs({ role: "ai", kind: "llm-in", text: label, detail: p.content || "", agentName: p.agent_name || "" });
    }
    if (type === "llm_response") {
      let body = p.response_text || "";
      if (p.thinking_text) body = `[Thinking]\n${p.thinking_text}\n\n[Response]\n${body}`;
      return withTs({ role: "ai", kind: "llm-out", text: "LLM Response", detail: body, agentName: p.agent_name || "" });
    }
    if (type === "log") return null;
    return withTs({ role: "ai", kind: "raw", text: `${type} ${JSON.stringify(p).slice(0, 300)}` });
  }

  function appendChat(item) {
    const box = $("#chat-stream");
    if (!box) return;
    if (!item) return;
    const el = document.createElement("div");
    el.className = `chat-bubble ${item.role === "user" ? "b-user" : item.role === "system" ? "b-system" : "b-ai"} kind-${item.kind || "raw"}`;
    const agentLabel = item.agentName && item.agentName !== "main_controller"
      ? ` · ${esc(item.agentName)}` : "";
    const meta =
      item.role === "user"
        ? "您"
        : item.role === "system"
        ? "系统"
        : item.kind === "thought"
        ? `主控 · 思考${agentLabel}`
        : item.kind === "tool"
        ? `主控 · 工具${agentLabel}`
        : item.kind === "validation"
        ? `主控 · 验证${agentLabel}`
        : item.kind === "report"
        ? `主控 · 报告${agentLabel}`
        : item.kind === "llm-iter"
        ? `LLM${agentLabel} · 迭代`
        : item.kind === "llm-in"
        ? `LLM${agentLabel} · 输入`
        : item.kind === "llm-out"
        ? `LLM${agentLabel} · 响应`
        : `主控`;
    // 可折叠详情面板：用于 LLM Input / Response 等大文本内容
    const detailHtml = item.detail
      ? `<details class="bubble-detail"><summary>展开详情</summary><pre>${esc(item.detail)}</pre></details>`
      : "";
    // 报告/结果类消息：正文较大，用可折叠面板包裹，默认折叠避免刷屏
    const isLong = item.kind === "report" || item.kind === "result";
    const bodyHtml = isLong
      ? `<details class="bubble-detail bubble-report"${
          item.kind === "report" ? " open" : ""
        }><summary>${esc(item.title || (item.kind === "report" ? "安全评估报告" : "阶段性结果"))}</summary><pre>${esc(
          item.text || ""
        )}</pre></details>`
      : `${esc(item.text || "")}${item.err ? '<span class="bubble-err"> ⚠</span>' : ""}${detailHtml}`;
    el.innerHTML = `
      <div class="bubble-meta"><span class="bubble-meta-label">${esc(meta)}</span><span class="bubble-time" title="${esc(
        item.created_at || ""
      )}">${esc(fmtClock(item.created_at || item.ts || item.time || item.at))}</span></div>
      <div class="bubble-body">${bodyHtml}</div>`;
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
  }

  async function loadConsoleApprovals(id) {
    const box = $("#approval-list-console");
    if (!box) return;
    try {
      const list = await api("/approvals?task_id=" + id);
      if (!list.length) {
        box.innerHTML = '<div class="empty" style="padding:14px">暂无审批请求</div>';
        return;
      }
      box.innerHTML = list
        .map(
          (a) => `
        <div class="approval-item st-${a.status}">
          <div class="ap-row">
            <span class="ap-tool">${esc(a.tool_name || "")}</span>
            <span class="ap-risk risk-${a.risk_level}">${esc(a.risk_level || "")}</span>
          </div>
          <div class="ap-detail">${esc(a.detail || a.tool_args || "")}</div>
          <div class="ap-foot">
            <span class="ap-state">${approvalStateLabel(a.status)}</span>
            ${
              a.status === "pending"
                ? `<span class="ap-actions">
                    <button class="btn tiny approve" data-ap="approve" data-id="${a.id}">批准</button>
                    <button class="btn tiny danger" data-ap="reject" data-id="${a.id}">拒绝</button>
                  </span>`
                : ""
            }
          </div>
        </div>`
        )
        .join("");
      box.querySelectorAll("[data-ap]").forEach((b) => {
        b.addEventListener("click", async () => {
          try {
            await api("/approvals/" + b.dataset.id + "/decision", {
              method: "POST",
              body: JSON.stringify({ decision: b.dataset.ap }),
            });
            toast(b.dataset.ap === "approve" ? "已批准" : "已拒绝");
            loadConsoleApprovals(id);
          } catch (e) {
            toast(e.message, true);
          }
        });
      });
    } catch (e) {
      box.innerHTML = `<div class="empty" style="padding:14px">审批加载失败：${esc(e.message)}</div>`;
    }
  }
  function approvalStateLabel(s) {
    return { pending: "待审批", approved: "已批准", rejected: "已拒绝", expired: "已过期" }[s] || s;
  }

  // 控制台实时流：SSE 联动
  function startConsoleStream(taskId) {
    closeStream();
    const onEvent = (ev) => {
      // 任务终态推送：刷新徽章、按钮、禁用输入框，并关闭 SSE
      if (ev.type === "task_status_changed") {
        const newStatus = ev.new_status;
        if (newStatus) refreshConsoleStatus(newStatus);
        return;
      }
      if (ev.type === "plan_step") {
        applyPlanStep(ev);
        return;
      }
      if (ev.type === "snapshot") {
        return;
      }
      // 对话流
      const chat = eventToChat(ev);
      if (chat) appendChat(chat);
      // 阶段/智能体刷新
      const p = ev.payload || {};
      if (ev.type === "phase_changed" && p.new_phase) {
        const ph = $("#phase-value");
        if (ph) ph.textContent = p.new_phase;
      }
      if (ev.type === "agent_start" && p.agent_name) {
        const pa = $("#phase-agent");
        if (pa) pa.textContent = p.agent_name;
      }
    };

    // 初始事件（live.recent_events 已渲染），此处仅处理增量
    try {
      const esUrl =
        `/api/v1/tasks/${taskId}/stream` + (state.token ? `?token=${encodeURIComponent(state.token)}` : "");
      const es = new EventSource(esUrl, { withCredentials: true });
      state.es = es;
      es.onmessage = (e) => {
        try {
          onEvent(JSON.parse(e.data));
        } catch {}
      };
      es.addEventListener("ping", () => {});
      // 断线兜底：原生 EventSource 在服务端显式 close 后 readyState=CLOSED，
      // 浏览器不再自动重连；如果只是网络抖动则 readyState=CONNECTING，
      // 浏览器会自己做指数退避。我们只在 CLOSED 时介入：
      //   1) 先 GET 一次 /tasks/:id 把 PG 真实状态拉回来，覆盖 SSE 漏掉的中间态；
      //   2) 主动 close + 短暂退避后重新订阅，避免死循环打爆后端。
      let resyncing = false;
      es.onerror = async () => {
        if (es.readyState !== EventSource.CLOSED) return; // 仍在重连中，不插手
        if (resyncing) return;
        resyncing = true;
        try {
          const detail = await api(`/tasks/${taskId}`);
          if (detail && detail.status) refreshConsoleStatus(detail.status);
        } catch {}
        try { es.close(); } catch {}
        // 仅当用户仍在同一个控制台时重连；否则保持关闭。
        setTimeout(() => {
          if (state.es === es && state.consoleId === taskId) {
            try { startConsoleStream(taskId); } catch {}
          }
        }, 1500);
      };
    } catch (err) {
      /* 实时流不可用时静默，initial 数据已展示 */
    }
  }

  /**
   * 收到 task_status_changed 时同步控制台徽章、按钮组与输入框可用性。
   * 同时刷新看板（loadDashboard）以保持活跃/历史区一致。
   */
  function refreshConsoleStatus(newStatus) {
    if (!["completed", "failed", "cancelled", "running", "queued"].includes(newStatus)) return;

    // 更新徽章（标题行第一个 statusBadge 占位由 statusBadge 生成）
    const titleRow = document.querySelector(".console-title-row");
    if (titleRow) {
      // 清掉旧徽章并插入新徽章
      const old = titleRow.querySelector(".badge");
      const fresh = statusBadge(newStatus);
      if (old) {
        old.outerHTML = fresh;
      } else {
        titleRow.insertAdjacentHTML("afterbegin", fresh);
      }
    }

    const canControl = ["queued", "running"].includes(newStatus);
    // 切换按钮组：中断（canControl 时） vs 继续（终态时）
    const actions = document.querySelector(".console-head-actions");
    if (actions) {
      const btnCancel = actions.querySelector("[data-console-cancel]");
      const btnEnqueue = actions.querySelector("[data-console-enqueue]");
      if (canControl) {
        if (!btnCancel) {
          actions.insertAdjacentHTML(
            "afterbegin",
            `<button class="btn danger small" data-console-cancel>中断</button>`
          );
        }
        if (btnEnqueue) btnEnqueue.remove();
      } else {
        if (btnCancel) btnCancel.remove();
        if (!btnEnqueue) {
          actions.insertAdjacentHTML(
            "beforeend",
            `<button class="btn small" data-console-enqueue>继续</button>`
          );
        }
      }
    }

    // 输入框可用性
    const inp = $("#instr-input");
    const btn = $("#instr-send");
    if (inp) inp.disabled = !canControl;
    if (btn) btn.disabled = !canControl;
    // 同步底部提示行
    const hint = document.querySelector(".console-input-hint");
    if (canControl) {
      if (hint) hint.remove();
    } else if (!hint) {
      const input = document.querySelector(".console-input");
      if (input) {
        input.insertAdjacentHTML(
          "afterend",
          '<div class="console-input-hint">任务非运行中，指令将在继续后生效</div>'
        );
      }
    }

    // 终态：关闭 SSE，避免无谓重连；后端 stream.py 已会 break，但保险起见本地也关
    if (["completed", "failed", "cancelled"].includes(newStatus)) {
      // 延迟一点以便把 last event 派完
      setTimeout(() => closeStream(), 50);
    }

    // 看板同步：避免活跃列表与控制台徽章不一致
    if (typeof loadDashboard === "function") loadDashboard();
  }

  // plan_step 增量更新（按 step_id 幂等）
  function applyPlanStep(ev) {
    const p = ev.payload || {};
    const planList = $("#plan-list");
    if (!planList || !p.step_id) return;
    let row = planList.querySelector(`[data-step="${cssEsc(p.step_id)}"]`);
    if (!row) {
      row = document.createElement("div");
      row.dataset.step = p.step_id;
      row.className = "plan-step";
      row.innerHTML = `
        <span class="plan-dot"></span>
        <span class="plan-seq"></span>
        <span class="plan-title"></span>
        <span class="plan-state"></span>`;
      // 按 seq 插入到正确位置，避免实时事件乱序追加导致步骤错排
      const seq = typeof p.seq === "number" ? p.seq : null;
      if (seq === null || seq < 0) {
        planList.appendChild(row);
      } else {
        const rows = planList.querySelectorAll(".plan-step");
        let inserted = false;
        for (const r of rows) {
          const rseq = Number(r.dataset.seq);
          if (!isNaN(rseq) && rseq > seq) {
            planList.insertBefore(row, r);
            inserted = true;
            break;
          }
        }
        if (!inserted) planList.appendChild(row);
      }
    }
    // 记录序号，供后续插位排序使用
    if (typeof p.seq === "number") row.dataset.seq = p.seq;
    row.className = `plan-step st-${p.status}`;
    if (typeof p.seq === "number" && p.seq >= 0) {
      row.querySelector(".plan-seq").textContent = p.seq + 1;
    }
    if (p.title) row.querySelector(".plan-title").textContent = p.title;
    row.querySelector(".plan-state").textContent = planStateLabel(p.status);
    // 刷新进度（以事件携带的总/完成数为准，避免新增行污染计数）
    refreshPlanProgress(planList, p.total, p.completed);
  }
  function refreshPlanProgress(planList, totalHint, completedHint) {
    const rows = planList.querySelectorAll(".plan-step");
    const completedRows = planList.querySelectorAll(".plan-step.st-completed").length;
    let total = totalHint != null ? totalHint : rows.length;
    let completed = completedHint != null ? completedHint : completedRows;
    // 运行时若实际完成行更多，以实际为准
    if (completedRows > completed) completed = completedRows;
    const prog = $("#plan-progress");
    if (prog) prog.textContent = `${completed}/${total}`;
    const bar = $("#plan-bar-fill");
    if (bar) bar.style.width = (total ? Math.round((completed / total) * 100) : 0) + "%";
  }
  function cssEsc(s) {
    return (s || "").replace(/"/g, '\\"');
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
      // EventSource 无法设置 Authorization 头，token 通过 ?token= 传递（后端 get_current_user_from_query 兼容）
      const esUrl = `/api/v1/tasks/${taskId}/stream` + (state.token ? `?token=${encodeURIComponent(state.token)}` : "");
      const es = new EventSource(esUrl, {
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

  // ---- 健康检查视图 ----
  const HEALTH_CHECKS = ["worker", "kali", "llm", "probe"];
  const healthState = { worker: null, kali: null, llm: null, probe: null };

  function setBadge(cardEl, state, text) {
    const badge = cardEl.querySelector("[data-badge]");
    if (!badge) return;
    badge.dataset.state = state; // ok | warn | err | idle
    badge.textContent = text;
  }
  function setDetail(cardEl, html) {
    const d = cardEl.querySelector("[data-detail]");
    if (d) d.innerHTML = html;
  }
  function getCard(key) {
    return $(`.health-card[data-check="${key}"]`);
  }

  function renderCheck(key, ok, detailHtml, badgeText) {
    const card = getCard(key);
    if (!card) return;
    const state = ok === null ? "idle" : ok ? "ok" : "err";
    setBadge(card, state, badgeText || (ok === null ? "检测中" : ok ? "正常" : "异常"));
    if (detailHtml != null) setDetail(card, detailHtml);
    healthState[key] = ok;
    renderOverview();
  }

  function renderOverview() {
    const ov = $("#health-overview");
    const txt = $("#health-overview-text");
    if (!ov || !txt) return;
    // probe 段为「未探测」时不计入失败判定
    const active = HEALTH_CHECKS.filter((k) => healthState[k] !== null);
    const allOk = active.length > 0 && active.every((k) => healthState[k] === true);
    const anyErr = active.some((k) => healthState[k] === false);
    let state, label;
    if (active.length === 0) {
      state = "unknown";
      label = "检测中…";
    } else if (anyErr) {
      state = "err";
      label = "链路异常，存在不通的环节";
    } else if (allOk) {
      state = "ok";
      label = "整体链路健康";
    } else {
      state = "warn";
      label = "部分环节未检测";
    }
    ov.dataset.state = state;
    txt.textContent = label;
  }

  async function loadHealth() {
    // 重置三段实时状态为检测中；probe 段保留「未探测」初始文案
    ["worker", "kali", "llm"].forEach((k) => {
      healthState[k] = null;
      const card = getCard(k);
      if (card) { setBadge(card, "idle", "检测中"); setDetail(card, "—"); }
    });
    renderOverview();
    // 并行拉取三段状态
    Promise.allSettled([
      api("/system/worker-status").then((d) => {
        const ok = d.available && d.online;
        const det = `在线：${d.online ? "是" : "否"}<br>队列积压：${d.queue_depth ?? "—"}` +
          (d.detail ? `<br><span class="health-mono">${esc(d.detail).slice(0, 120)}</span>` : "");
        renderCheck("worker", ok, det, ok ? "在线" : "离线");
      }),
      api("/system/kali-status").then((d) => {
        const ok = d.available && d.healthy;
        const det = `容器：${d.container_id ?? "—"}<br>退出码：${d.exit_code ?? "—"}` +
          (d.stdout ? `<br><span class="health-mono">${esc(d.stdout).slice(0, 120)}</span>` : "") +
          (d.error ? `<br><span class="health-err">${esc(d.error)}</span>` : "");
        renderCheck("kali", ok, det, ok ? "健康" : "异常");
      }),
      api("/system/llm-status").then((d) => {
        const ok = d.available && d.healthy;
        const det = `模型：${d.model ?? "—"}<br>延迟：${d.latency_ms ?? "—"} ms<br>回复：${esc(d.reply ?? "—")}` +
          (d.error ? `<br><span class="health-err">${esc(d.error)}</span>` : "");
        renderCheck("llm", ok, det, ok ? "连通" : "异常");
      }),
    ]).catch(() => {});
    // probe 段历史
    loadLastProbe();
  }

  async function loadLastProbe() {
    const card = $("#last-probe-card");
    if (!card) return;
    try {
      const tasks = await api("/tasks?limit=50");
      const probe = (tasks || []).filter((t) => t.kind === "probe").sort(
        (a, b) => new Date(b.created_at) - new Date(a.created_at)
      )[0];
      if (!probe) {
        card.innerHTML = `<div class="empty">尚未进行过探测</div>`;
        const t = $("#last-probe-time");
        if (t) t.textContent = "";
        return;
      }
      renderLastProbeCard(probe);
    } catch (err) {
      card.innerHTML = `<div class="empty">加载失败：${esc(err.message)}</div>`;
    }
  }

  async function renderLastProbeCard(probe) {
    const card = $("#last-probe-card");
    const timeEl = $("#last-probe-time");
    // 若仍在进行中，补充一次轮询
    let task = probe;
    if (task.status !== "completed" && task.status !== "failed" && task.status !== "cancelled") {
      try {
        task = await api(`/tasks/${task.id}`);
      } catch (_) {}
    }
    const ok = task.status === "completed";
    const st = ok ? "ok" : task.status === "failed" ? "err" : "warn";
    if (timeEl) timeEl.textContent = "时间：" + fmtDate(task.created_at);
    card.innerHTML = `
      <div class="last-probe-row">
        <span class="health-badge" data-state="${st}">${ok ? "成功" : task.status === "failed" ? "失败" : "进行中"}</span>
        <span class="last-probe-name">${esc(task.name)}</span>
      </div>
      <div class="last-probe-result">
        ${task.result ? esc(task.result) : (task.error ? `<span class="health-err">${esc(task.error)}</span>` : "（无结论）")}
      </div>
      <div class="last-probe-meta">任务 ID：${esc(task.id)}</div>`;
  }

  function openProbeModal(targets) {
    const opts = targets.map((t) => `<option value="${t.id}">${esc(t.name)} — ${esc(t.url)}</option>`).join("");
    const body = `
      <h3>发起健康探测</h3>
      <p class="modal-sub">选择已授权目标，Worker 将在共享 Kali 沙箱中对目标做一次连通性验证（curl 访问），并由模型给出结论。</p>
      <form id="probe-form" class="auth-form">
        <label>目标
          <select name="target_id" required ${targets.length ? "" : "disabled"}>
            ${targets.length ? opts : '<option>无可用目标</option>'}
          </select>
        </label>
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-action="cancel-modal">取消</button>
          <button type="submit" class="btn primary" ${targets.length ? "" : "disabled"}>发起探测</button>
        </div>
      </form>`;
    openModal(body);
    const form = $("#probe-form");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const targetId = fd.get("target_id");
      if (!targetId) return;
      closeModal();
      try {
        const resp = await api("/system/probe", { method: "POST", body: { target_id: targetId } });
        toast("探测任务已派发，正在执行…");
        // 标记 probe 段为进行中
        renderCheck("probe", null, "探测任务执行中…", "进行中");
        pollProbeResult(resp.task_id);
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  async function runProbe() {
    try {
      const targets = await api("/targets");
      if (!targets || !targets.length) {
        toast("请先在「授权目标」中创建目标后再探测", true);
        return;
      }
      openProbeModal(targets);
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function pollProbeResult(taskId) {
    const card = getCard("probe");
    const deadline = Date.now() + 110000; // 略大于后端 90s 硬超时
    const tick = async () => {
      try {
        const t = await api(`/tasks/${taskId}`);
        if (t.status === "completed" || t.status === "failed" || t.status === "cancelled") {
          const ok = t.status === "completed";
          const detail = ok
            ? `<span class="health-ok">${esc(t.result || "目标可达")}</span>`
            : `<span class="health-err">${esc(t.error || t.result || "探测失败")}</span>`;
          renderCheck("probe", ok, detail, ok ? "通过" : "失败");
          loadLastProbe();
          return;
        }
        if (card) setDetail(card, "探测任务执行中…（" + t.status + "）");
      } catch (err) {
        if (card) setDetail(card, `<span class="health-err">${esc(err.message)}</span>`);
      }
      if (Date.now() > deadline) {
        renderCheck("probe", false, "探测超时（>110s），请检查 Worker 状态", "超时");
        return;
      }
      setTimeout(tick, 2000);
    };
    tick();
  }

  // ---- 初始化 ----
  function init() {
    try {
      bindAuth();
      bindNav();
    } catch (err) {
      console.error("初始化事件绑定失败:", err);
    }
    if (state.token) {
      enterApp().catch(() => showLogin());
    } else {
      showLogin();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
