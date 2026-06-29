function toggleDropdown(btn) {
      const isOpen = btn.getAttribute('aria-expanded') === 'true';
      closeDropdowns();
      if (!isOpen) {
        btn.setAttribute('aria-expanded', 'true');
        document.getElementById('dropdown-cadastro').classList.add('open');
      }
    }

function closeDropdowns() {
    document.querySelectorAll('.dropdown-toggle').forEach(b => b.setAttribute('aria-expanded', 'false'));
    document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('open'));
}

function setActive(el) {
    document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
    el.classList.add('active');
}

document.addEventListener('click', function(e) {
    if (!e.target.closest('.nav-links li')) closeDropdowns();
});