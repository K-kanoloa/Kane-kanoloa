for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = '已复制';
      document.getElementById('status').textContent = '命令已复制';
      setTimeout(() => { button.textContent = '复制'; }, 1800);
    } catch {
      button.textContent = '请选中命令复制';
      document.getElementById('status').textContent = '请选中命令文本并复制';
    }
  });
}
