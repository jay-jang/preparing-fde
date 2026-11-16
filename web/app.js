document.querySelectorAll('pre').forEach((pre) => {
  const code = pre.querySelector('code');
  if (!code || !navigator.clipboard) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy-code';
  button.textContent = '복사';
  button.setAttribute('aria-label', '코드 복사');
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(code.textContent);
      button.textContent = '복사됨';
    } catch {
      button.textContent = '직접 선택해 복사';
    }
    setTimeout(() => { button.textContent = '복사'; }, 2000);
  });
  pre.prepend(button);
});

document.querySelectorAll('.mobile-toc a').forEach((link) => {
  link.addEventListener('click', () => { link.closest('details').open = false; });
});

if ('IntersectionObserver' in window) {
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    if (!visible.length) return;
    const frame = visible[0].target.dataset.frame;
    document.querySelectorAll('.toc a[data-frame]').forEach((link) => {
      if (link.dataset.frame === frame) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  }, { rootMargin: '-5% 0px -65% 0px', threshold: 0 });
  document.querySelectorAll('article[data-frame]').forEach((frame) => observer.observe(frame));
}
