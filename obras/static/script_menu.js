// Funções de suporte à sidebar — mantidas aqui para carregamento antecipado
function setActive(el) {
    document.querySelectorAll('.sidebar-link').forEach(l => l.classList.remove('active'));
    if (el) el.classList.add('active');
}

function initSidebarMarquee() {
    document.querySelectorAll('.sidebar-link').forEach(function (link) {
        var textSpan = link.querySelector('.sidebar-link-text');
        if (!textSpan) return;
        // scrollWidth do link reflete o conteúdo real (sem clip); clientWidth é o espaço disponível.
        var overflow = link.scrollWidth - link.clientWidth;
        if (overflow <= 0) return;
        textSpan.style.setProperty('--marquee-offset', '-' + overflow + 'px');
        textSpan.classList.add('marquee');
    });
}

document.addEventListener('DOMContentLoaded', initSidebarMarquee);
