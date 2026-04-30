if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/ui/sw.js', { scope: '/ui/' });
}
