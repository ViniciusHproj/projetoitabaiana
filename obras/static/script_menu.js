// Funções de suporte à sidebar — mantidas aqui para carregamento antecipado
function setActive(el) {
    document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
    if (el) el.classList.add('active');
}
