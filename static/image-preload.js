// Descarga todas las imágenes de las preguntas al entrar (con reintentos) y
// las guarda en memoria, para que al abrir cada pregunta se pinten al
// instante sin volver a pedir nada al servidor.
const ImagePreloader = (() => {
  const cache = new Map();
  let started = null;

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function fetchWithRetry(url, attempts = 5) {
    for (let i = 0; i < attempts; i++) {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        const res = await fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
        if (res.ok) return res;
      } catch (_) {
        // red caída o error puntual: se reintenta
      }
      await wait(500 * (i + 1));
    }
    return null;
  }

  function preloadAll(onProgress) {
    if (started) return started;
    started = (async () => {
      const res = await fetchWithRetry('/api/image-manifest');
      if (!res) {
        started = null;
        return;
      }
      const { urls = [] } = await res.json();
      const queue = urls.filter((url) => !cache.has(url));
      let done = urls.length - queue.length;
      onProgress?.(done, urls.length);

      const worker = async () => {
        while (queue.length) {
          const url = queue.shift();
          const imgRes = await fetchWithRetry(url);
          if (imgRes) cache.set(url, URL.createObjectURL(await imgRes.blob()));
          done++;
          onProgress?.(done, urls.length);
        }
      };
      await Promise.all([worker(), worker()]);
    })();
    return started;
  }

  // URL local ya descargada si la hay; si no, la original (la pide el navegador).
  const src = (url) => (url && cache.get(url)) || url;

  return { preloadAll, src };
})();
