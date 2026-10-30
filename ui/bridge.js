/* ============================================================
   桌面壳桥接层
   ------------------------------------------------------------
   Agent Hub 桌面版不监听任何端口。页面里的 fetch('/api/...')
   在这里被改写成一次 WebView2 WebMessage，直达 Python 进程内
   的 ROUTES 分发（server.dispatch_api）。

   浏览器开发模式（没有 window.pywebview）时原样放行，
   因此 tests/ 下的浏览器回归不受影响。

   必须早于 app.js 加载：index.html 中它排在 app.js 之前，
   defer 脚本按文档顺序执行。
   ============================================================ */
(function () {
  "use strict";
  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  if (!realFetch) return;

  // 超时兜底需要定时器。个别宿主（node 契约测试的裸 VM）没有 window.setTimeout：
  // 检测不到就退化为不超时，由调用方（app.js api() 的 120s AbortController）兜底。
  var scheduleTimeout = typeof window.setTimeout === "function" ? function (fn, ms) { return window.setTimeout(fn, ms); } : null;
  var cancelTimeout = typeof window.clearTimeout === "function" ? function (id) { window.clearTimeout(id); } : null;

  function apiBase() {
    var api = window.pywebview && window.pywebview.api;
    return api && typeof api.request === "function" ? api : null;
  }

  function toPath(input) {
    if (typeof input === "string") return input;
    if (input && typeof input.url === "string") return input.url;
    return "";
  }

  // Request 对象的 body 需要异步读取，这条路很少用到，直接放行给原生 fetch。
  function isStreamBody(body) {
    return !!body && typeof body === "object" && typeof body.text === "function";
  }

  // 命名函数，便于 --smoke 自检确认 fetch 确实被改写
  window.fetch = function bridgedFetch(input, init) {
    init = init || {};
    var path = toPath(input);
    var api = apiBase();
    if (!api || path.indexOf("/api/") !== 0) return realFetch(input, init);

    var method = (init.method || "GET").toUpperCase();
    var body = init.body;
    if (isStreamBody(body)) return realFetch(input, init);
    if (typeof body === "string" && body) {
      try { body = JSON.parse(body); } catch (e) { body = null; }
    }
    if (body === undefined) body = null;

    var signal = init.signal;
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = null;
      var clearGuard = function () { if (timer !== null && cancelTimeout) cancelTimeout(timer); timer = null; };
      function fail(err) { if (!settled) { settled = true; clearGuard(); reject(err); } }
      function ok(res) { if (!settled) { settled = true; clearGuard(); resolve(res); } }

      if (signal) {
        if (signal.aborted) {
          fail(new DOMException("The operation was aborted.", "AbortError"));
          return;
        }
        signal.addEventListener("abort", function () {
          fail(new DOMException("The operation was aborted.", "AbortError"));
        }, { once: true });
      }

      // pywebview promise 挂起时 fetch 永不 settle，这里给桥自身的兜底超时。
      // 60s 的依据：比 app.js api() 的 120s 短（先触发、文案更可行动），比最慢的
      // 合法调用（批量检测首帧 8~9s，见 workbench.js 注释）长约 7 倍。
      // 超时只是放弃等待：Python 侧可能仍在执行，文案如实说明并劝阻重复提交。
      // Request 流式 body 的路径在进入本 Promise 前已放行给原生 fetch，不受影响。
      if (scheduleTimeout) {
        // A confirmed model summary may start the gateway and then wait for an
        // upstream response. Keep the ordinary bridge guard unchanged.
        var guardMs = path === "/api/sessions/summary/generate" ? 115000 : 60000;
        timer = scheduleTimeout(function () {
          fail(new Error("本地服务 " + Math.round(guardMs / 1000) + " 秒未响应，已放弃等待。操作可能仍在后台执行，请稍后刷新确认，勿重复提交。"));
        }, guardMs);
      }
      api.request(method, path, body).then(function (payload) {
        var status = payload && typeof payload.status === "number" ? payload.status : 200;
        var data = payload && Object.prototype.hasOwnProperty.call(payload, "body") ? payload.body : payload;
        ok(new Response(JSON.stringify(data), {
          status: status,
          headers: { "Content-Type": "application/json; charset=utf-8" }
        }));
      }).catch(function (err) {
        fail(err instanceof Error ? err : new Error(String(err)));
      });
    });
  };

  /* —— 外链接管 ——
     桌面壳没有地址栏。target="_blank" 的链接（如 WorkBuddy 官方
     OAuth 登录页）交给系统默认浏览器，而不是在壳内再开一个窗口。 */
  document.addEventListener("click", function (event) {
    var api = apiBase();
    if (!api) return;
    var anchor = event.target && event.target.closest ? event.target.closest('a[target="_blank"]') : null;
    if (!anchor) return;
    var href = anchor.getAttribute("href") || "";
    if (href.indexOf("http://") !== 0 && href.indexOf("https://") !== 0) return;
    event.preventDefault();
    api.openExternal(href);
  }, true);
})();
