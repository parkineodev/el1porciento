// Conexión con el servidor a prueba de cortes, común a móvil, pantalla y
// presentador: cada petición tiene un límite de espera, los fallos de red y
// del servidor se reintentan solos con esperas crecientes, y el sondeo nunca
// lanza una consulta nueva mientras la anterior sigue en el aire.
const Net = (() => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  class HttpError extends Error {
    constructor(status, detail) {
      super(detail || (status ? `Error ${status}` : 'Sin conexión'));
      this.status = status; // 0 = no hubo respuesta (red caída o tiempo agotado)
      this.detail = detail || null;
    }
  }

  // 0: sin respuesta. 408/425/429 y 5xx: el servidor está ocupado o
  // reiniciándose. Todo lo demás (400, 401, 404...) es una respuesta
  // definitiva y no se reintenta.
  const isRetryable = (status) => status === 0 || status === 408 || status === 425 || status === 429 || status >= 500;

  let lastOkAt = Date.now();
  let lastFailAt = 0;

  async function request(url, options = {}) {
    const {
      method = 'GET',
      body,
      timeoutMs = 8000,
      retries = 0,
      baseDelayMs = 500,
      maxDelayMs = 4000,
      shouldContinue,
    } = options;
    for (let attempt = 0; ; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      let status = 0;
      let data = null;
      try {
        const res = await fetch(url, {
          method,
          headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
          body: body !== undefined ? JSON.stringify(body) : undefined,
          signal: controller.signal,
          cache: 'no-store',
        });
        status = res.status;
        data = await res.json().catch(() => null);
      } catch (_) {
        // red caída o tiempo agotado: status queda en 0
      } finally {
        clearTimeout(timer);
      }

      if (status >= 200 && status < 300) {
        lastOkAt = Date.now();
        return data;
      }
      if (status) lastOkAt = Date.now(); // hay conexión, aunque la respuesta sea un error
      const detail = data && typeof data.detail === 'string' ? data.detail : null;
      if (!isRetryable(status)) throw new HttpError(status, detail);
      lastFailAt = Date.now();
      if (attempt >= retries || (shouldContinue && !shouldContinue())) throw new HttpError(status, detail);
      const delay = Math.min(maxDelayMs, baseDelayMs * 2 ** attempt);
      await wait(delay * (0.7 + Math.random() * 0.6));
    }
  }

  // Sondeo encadenado: la siguiente consulta sale cuando vuelve la anterior,
  // con algo de variación para que los móviles no pregunten todos a la vez, y
  // espaciándose si el servidor no contesta.
  function poll(fn, intervalMs) {
    let timer = null;
    let running = false;
    let stopped = false;
    let failures = 0;

    async function tick() {
      if (stopped || running) return;
      clearTimeout(timer);
      running = true;
      try {
        await fn();
        failures = 0;
      } catch (_) {
        failures++;
      }
      running = false;
      if (stopped) return;
      const base = failures ? Math.min(4000, intervalMs * 2 ** Math.min(failures, 3)) : intervalMs;
      timer = setTimeout(tick, base * (0.85 + Math.random() * 0.3));
    }

    tick();
    return {
      stop() {
        stopped = true;
        clearTimeout(timer);
      },
      // Consulta ya (al volver a la pestaña, tras enviar algo...).
      now() {
        if (!stopped && !running) tick();
      },
    };
  }

  // Aviso discreto solo si se lleva un rato largo sin poder hablar con el
  // servidor; los cortes cortos no se enseñan.
  function connectionBadge(afterMs = 10000) {
    const el = document.createElement('div');
    el.textContent = 'Reconectando…';
    el.setAttribute('role', 'status');
    el.style.cssText =
      'position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:9999;' +
      'padding:6px 14px;border-radius:999px;font:600 13px/1.2 system-ui,sans-serif;' +
      'background:rgba(0,0,0,.72);color:#fff;pointer-events:none;display:none;';
    const attach = () => document.body.appendChild(el);
    if (document.body) attach();
    else document.addEventListener('DOMContentLoaded', attach);
    setInterval(() => {
      const offline = lastFailAt > lastOkAt && Date.now() - lastOkAt > afterMs;
      el.style.display = offline ? 'block' : 'none';
    }, 1000);
  }

  return { request, poll, wait, HttpError, isRetryable, connectionBadge };
})();
