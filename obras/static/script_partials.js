/* ─────────────────────────────────────────────────────────────
   script_partials.js
   Scripts dos partials movidos para cá para CSP compliance.
   initPartial() é chamado pelo dispatcher em DOMContentLoaded
   e htmx:afterSwap — inicializa o partial que estiver no DOM.
   ───────────────────────────────────────────────────────────── */

/* ── Máscaras de entrada ── */
function mascaraCPF(input) {
  var v = input.value.replace(/\D/g, '');
  if (v.length > 11) v = v.slice(0, 11);
  v = v.replace(/(\d{3})(\d)/, '$1.$2');
  v = v.replace(/(\d{3})(\d)/, '$1.$2');
  v = v.replace(/(\d{3})(\d{1,2})$/, '$1-$2');
  input.value = v;
}

function mascaraRG(input) {
  var v = input.value.replace(/\D/g, '');
  if (v.length <= 9) {
    v = v.replace(/(\d{2})(\d)/, '$1.$2');
    v = v.replace(/(\d{3})(\d)/, '$1.$2');
    v = v.replace(/(\d{3})(\d{1})$/, '$1-$2');
  }
  input.value = v;
}

function mascaraCNPJ(campo) {
  var v = campo.value.replace(/\D/g, '');
  if (v.length > 14) v = v.slice(0, 14);
  v = v.replace(/^(\d{2})(\d)/, '$1.$2');
  v = v.replace(/^(\d{2})\.(\d{3})(\d)/, '$1.$2.$3');
  v = v.replace(/\.(\d{3})(\d)/, '.$1/$2');
  v = v.replace(/(\d{4})(\d)/, '$1-$2');
  campo.value = v;
}

function mascaraValorBRL() {
  var v = this.value.replace(/\D/g, '');
  if (!v) { this.value = ''; return; }
  var n = parseInt(v) / 100;
  this.value = n.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/* ── Tabs de funcionário (cadastro) ── */
function mostrarAbaFunc(aba) {
  var btnDados = document.getElementById('btn-dados');
  var btnLogin = document.getElementById('btn-login');
  var divDados = document.getElementById('aba-dados');
  var divLogin = document.getElementById('aba-login');
  if (!btnDados) return;
  if (aba === 'dados') {
    divDados.style.display = 'block';
    divLogin.style.display = 'none';
    btnDados.classList.add('tab-ativo');
    btnLogin.classList.remove('tab-ativo');
  } else {
    divDados.style.display = 'none';
    divLogin.style.display = 'block';
    btnLogin.classList.add('tab-ativo');
    btnDados.classList.remove('tab-ativo');
  }
}

/* ── Tabs de funcionário (edição) ── */
function mostrarAbaEdit(aba) {
  var btnDados = document.getElementById('btn-dados-edit');
  var btnLogin = document.getElementById('btn-login-edit');
  var divDados = document.getElementById('aba-dados-edit');
  var divLogin = document.getElementById('aba-login-edit');
  if (!btnDados) return;
  if (aba === 'dados') {
    divDados.style.display = 'block';
    divLogin.style.display = 'none';
    btnDados.classList.add('tab-ativo');
    btnLogin.classList.remove('tab-ativo');
  } else {
    divDados.style.display = 'none';
    divLogin.style.display = 'block';
    btnLogin.classList.add('tab-ativo');
    btnDados.classList.remove('tab-ativo');
  }
}

/* ── Login ── */
function initLogin() {
  var form = document.getElementById('form-login');
  if (!form) return;
  form.addEventListener('submit', function () {
    var btn = document.getElementById('btn-login-submit');
    if (btn) { btn.classList.add('htmx-request'); btn.disabled = true; }
  });
  var cpfInput = document.getElementById('id_username');
  if (cpfInput) cpfInput.addEventListener('input', function () { mascaraCPF(this); });
}

/* ── Busca de Funcionário ── */
function initBuscaFunc() {
  var inputCPF = document.getElementById('cpf-busca-func');
  if (!inputCPF) return;
  inputCPF.addEventListener('input', function () { mascaraCPF(this); });
}

/* ── Alerta flutuante no estilo partial_messages ── */
function _mostrarAlerta(mensagem, tipo) {
  tipo = tipo || 'error';
  var cores = {
    error:   { bg: '#f8d7da', color: '#721c24', border: '#f5c6cb' },
    warning: { bg: '#fff3cd', color: '#856404', border: '#ffeeba' },
    success: { bg: '#d4edda', color: '#155724', border: '#c3e6cb' },
    info:    { bg: '#d1ecf1', color: '#0c5460', border: '#bee5eb' }
  };
  var c = cores[tipo] || cores.error;
  var container = document.getElementById('container-mensagens');
  if (!container) {
    container = document.createElement('div');
    container.id = 'container-mensagens';
    container.style.cssText = 'position:fixed;top:85px;left:50%;transform:translateX(-50%);z-index:9999;width:100%;max-width:400px;display:flex;flex-direction:column;align-items:center;pointer-events:none;';
    document.body.appendChild(container);
  }
  var div = document.createElement('div');
  div.style.cssText = [
    'background:' + c.bg,
    'color:' + c.color,
    'border:1px solid ' + c.border,
    'padding:15px 25px',
    'border-radius:50px',
    'font-weight:bold',
    'box-shadow:0 10px 25px rgba(0,0,0,0.2)',
    'margin-bottom:10px',
    'display:flex',
    'align-items:center',
    'gap:10px',
    'pointer-events:auto',
    'animation:surgirESumir 4s ease-in-out forwards'
  ].join(';');
  div.textContent = mensagem;
  container.appendChild(div);
  setTimeout(function () { if (div.parentNode) div.parentNode.removeChild(div); }, 4000);
}

/* ── Cleanup de listeners globais entre trocas de partial ──
   initPartial chama _removerListenersGlobais() antes de cada init*,
   garantindo que não acumulem listeners no document a cada navegação HTMX. */
var _listenersGlobais = [];
function _addDocListener(tipo, fn) {
  document.addEventListener(tipo, fn);
  _listenersGlobais.push([tipo, fn]);
}
function _removerListenersGlobais() {
  _listenersGlobais.forEach(function (par) { document.removeEventListener(par[0], par[1]); });
  _listenersGlobais = [];
  // Desconecta o MutationObserver do dashboard ao sair — evita acúmulo por navegação HTMX
  if (_dashThemeObserver) { _dashThemeObserver.disconnect(); _dashThemeObserver = null; }
  // Remove tooltip flutuante que possa ter ficado preso no DOM após navegação HTMX
  var _orphanTooltip = document.querySelector('.exclusao-tooltip');
  if (_orphanTooltip) _orphanTooltip.remove();
}

/* ── Auto-retry em falhas de rede / 5xx ── */
function _configurarRetry(form, evento) {
  var tentativas = 0;
  var maxTentativas = 3;
  var delay = 3000;
  var indicatorSel = form.getAttribute('hx-indicator');

  function _indicator() {
    return indicatorSel ? document.querySelector(indicatorSel) : null;
  }


  function _agendar() {
    if (tentativas >= maxTentativas) {
      tentativas = 0;
      var el = _indicator();
      if (el) {
        var novo = el.cloneNode(false);
        novo.classList.remove('htmx-request');
        novo.setAttribute('type', 'button');
        novo.textContent = 'Recarregar Página';
        novo.style.cssText = 'background:#b91c1c;cursor:pointer;';
        novo.addEventListener('click', function () { window.location.reload(); }, { once: true });
        el.parentNode.replaceChild(novo, el);
      }
      _mostrarAlerta('Falha na comunicação com o servidor. Recarregue a página e tente novamente.', 'error');
      return;
    }
    tentativas++;
    setTimeout(function () { htmx.trigger(form, evento); }, delay);
  }

  form.addEventListener('htmx:sendError',    function () { _agendar(); if (tentativas > 0) { var el = _indicator(); if (el) el.classList.add('htmx-request'); } });
  form.addEventListener('htmx:responseError', function () { _agendar(); if (tentativas > 0) { var el = _indicator(); if (el) el.classList.add('htmx-request'); } });
  form.addEventListener('htmx:afterRequest',  function (evt) {
    if (evt.detail.successful) {
      tentativas = 0;
    } else if (tentativas > 0) {
      var el = _indicator();
      if (el) el.classList.add('htmx-request');
    }
  });
}

/* ── Cadastro de Obras ── */
function limparFormObras() {
  var campos = ['idObraManual','tipoObra','situacao','tipoExecucao','valorObra',
                'dataInicio','conclusaoPrevista','dataFinalizacao','nomeEmpresa',
                'endereco','cnpj_empresa','fotoObra'];
  campos.forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.value = '';
  });
}

function initCadastroObras() {
  var modal = document.getElementById('modal-confirma-cadastro');
  var form  = document.getElementById('form-cadastro-obras');
  if (!modal || !form) return;
  _configurarRetry(form, 'confirmar-cadastro');

  function abrirModal() { modal.style.display = 'flex'; document.getElementById('modal-confirmar').focus(); }
  function fecharModal() { modal.style.display = 'none'; }

  document.getElementById('btn-limpar-obras').addEventListener('click', limparFormObras);
  document.getElementById('btn-salvar').addEventListener('click', abrirModal);
  document.getElementById('modal-confirmar').addEventListener('click', function () { fecharModal(); htmx.trigger(form, 'confirmar-cadastro'); });
  document.getElementById('modal-cancelar').addEventListener('click', fecharModal);
  document.getElementById('modal-backdrop').addEventListener('click', fecharModal);
  _addDocListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

  var valorInput = document.getElementById('valorObra');
  if (valorInput) valorInput.addEventListener('input', mascaraValorBRL);

  var cnpjInput = document.getElementById('cnpj_empresa');
  if (cnpjInput) cnpjInput.addEventListener('input', function () { mascaraCNPJ(this); });
}

/* ── Cadastro de Funcionário ── */
function initCadastroFunc() {
  var modal = document.getElementById('modal-cadastro-func');
  var form  = document.getElementById('form-cadastro-func');
  if (!modal || !form) return;
  _configurarRetry(form, 'confirmar-cadastro-func');

  function abrirModal() { modal.style.display = 'flex'; document.getElementById('modal-confirmar-func').focus(); }
  function fecharModal() { modal.style.display = 'none'; }

  document.getElementById('btn-dados').addEventListener('click', function () { mostrarAbaFunc('dados'); });
  document.getElementById('btn-login').addEventListener('click', function () { mostrarAbaFunc('login'); });
  document.getElementById('btn-proximo-func').addEventListener('click', function () { mostrarAbaFunc('login'); });
  document.getElementById('btn-voltar-func').addEventListener('click', function () { mostrarAbaFunc('dados'); });
  document.getElementById('btn-salvar').addEventListener('click', abrirModal);
  document.getElementById('btn-fechar-modal-func').addEventListener('click', fecharModal);
  document.getElementById('modal-confirmar-func').addEventListener('click', function () { fecharModal(); htmx.trigger(form, 'confirmar-cadastro-func'); });
  document.getElementById('modal-backdrop-func').addEventListener('click', fecharModal);
  _addDocListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

  var rgInput  = document.getElementById('rg-cadastro-func');
  var cpfInput = document.getElementById('cpf-cadastro-func');
  if (rgInput)  rgInput.addEventListener('input',  function () { mascaraRG(this); });
  if (cpfInput) cpfInput.addEventListener('input', function () { mascaraCPF(this); });

  /* Aba inicial: Django injeta data-initial-tab no .tab-row quando há erro de validação */
  var tabRow = document.getElementById('tab-row-func');
  if (tabRow && tabRow.dataset.initialTab) mostrarAbaFunc(tabRow.dataset.initialTab);
}

/* ── Edição de Obra ── */
function initEditaObra() {
  var modal = document.getElementById('modal-edita-obra');
  var form  = document.getElementById('form-edita-obra');
  if (!modal || !form) return;
  _configurarRetry(form, 'confirmar-edita-obra');

  function abrirModal() { modal.style.display = 'flex'; document.getElementById('modal-confirmar-edita-obra').focus(); }
  function fecharModal() { modal.style.display = 'none'; }

  document.getElementById('btn-cancelar-edita-obra').addEventListener('click', function () { window.location.reload(); });
  document.getElementById('btn-salvar-alteracoes').addEventListener('click', abrirModal);
  document.getElementById('btn-fechar-modal-edita-obra').addEventListener('click', fecharModal);
  document.getElementById('modal-confirmar-edita-obra').addEventListener('click', function () { fecharModal(); htmx.trigger(form, 'confirmar-edita-obra'); });
  document.getElementById('modal-backdrop-edita-obra').addEventListener('click', fecharModal);
  _addDocListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

  var valorInput = document.getElementById('valorObraEdit');
  if (valorInput) valorInput.addEventListener('input', mascaraValorBRL);

  var cnpjInput = document.getElementById('cnpj_edit');
  if (cnpjInput) cnpjInput.addEventListener('input', function () { mascaraCNPJ(this); });
}

/* ── Edição de Funcionário ── */
function initEditaFunc() {
  var modal = document.getElementById('modal-edita-func');
  var form  = document.getElementById('form-edita-func');
  if (!modal || !form) return;
  _configurarRetry(form, 'confirmar-edita-func');

  function abrirModal() { modal.style.display = 'flex'; document.getElementById('modal-confirmar-edita-func').focus(); }
  function fecharModal() { modal.style.display = 'none'; }

  document.getElementById('btn-dados-edit').addEventListener('click', function () { mostrarAbaEdit('dados'); });
  document.getElementById('btn-login-edit').addEventListener('click', function () { mostrarAbaEdit('login'); });
  document.getElementById('btn-proximo-edita-func').addEventListener('click', function () { mostrarAbaEdit('login'); });
  document.getElementById('btn-voltar-edita-func').addEventListener('click', function () { mostrarAbaEdit('dados'); });
  document.getElementById('btn-salvar-alteracoes').addEventListener('click', abrirModal);
  document.getElementById('btn-fechar-modal-edita-func').addEventListener('click', fecharModal);
  document.getElementById('modal-confirmar-edita-func').addEventListener('click', function () { fecharModal(); htmx.trigger(form, 'confirmar-edita-func'); });
  document.getElementById('modal-backdrop-edita-func').addEventListener('click', fecharModal);
  _addDocListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

  var rgInput = document.getElementById('rg-edita-func');
  if (rgInput) rgInput.addEventListener('input', function () { mascaraRG(this); });
}

/* ── Dashboard de Obras ── */
var _dashCharts = [];
var _dashThemeObserver = null;

function _destruirDashCharts() {
  _dashCharts.forEach(function(c) { try { c.destroy(); } catch(e) {} });
  _dashCharts = [];
}

function initDashboard() {
  var el = document.getElementById('dashboard-dados');
  if (!el) return;

  _destruirDashCharts();

  /* Para quando o usuário trocar o tema, recriar os gráficos com as novas cores */
  if (_dashThemeObserver) _dashThemeObserver.disconnect();
  _dashThemeObserver = new MutationObserver(function(mutations) {
    mutations.forEach(function(m) {
      if (m.attributeName === 'data-theme' && document.getElementById('dashboard-dados')) {
        _destruirDashCharts();
        initDashboard();
      }
    });
  });
  _dashThemeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  var textColor  = isDark ? '#ffffff' : '#1a3050';
  var titleColor = isDark ? '#ffffff' : '#1a3050';
  var gridColor  = isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)';
  var bgCard     = isDark ? 'rgba(15,23,42,0.6)' : '#ffffff';

  Chart.defaults.color = textColor;
  Chart.defaults.font.family = "'DM Sans', sans-serif";
  Chart.defaults.font.size = 11;

  /* Cores semânticas fixas por significado */
  var COR_STATUS = {
    'Em andamento':                          '#16a34a',
    'Em licitação':                          '#0891b2',
    'Finalizada por conclusão de construção':'#2563eb',
    'Finalizada por distrato':               '#3b82f6',
    'Paralisada':                            '#d97706',
    'Cancelada':                             '#dc2626',
    'Não informado':                         '#94a3b8',
  };
  var CORES_PALETTE = ['#3b7dd8','#16a34a','#d97706','#dc2626','#7c3aed','#0891b2','#d35400','#059669','#7f8c8d','#c0392b'];

  function corPorLabel(label, idx) {
    return COR_STATUS[label] || CORES_PALETTE[idx % CORES_PALETTE.length];
  }

  /* Tooltip rico: percentual + valor */
  function tooltipRico(total) {
    return {
      callbacks: {
        label: function(ctx) {
          var val = ctx.parsed.x !== undefined ? ctx.parsed.x : ctx.parsed.y;
          if (val === undefined) val = ctx.parsed;
          var pct = total > 0 ? ((val / total) * 100).toFixed(1) : 0;
          return ' ' + ctx.dataset.label + ': ' + val + ' (' + pct + '%)';
        }
      },
      backgroundColor: isDark ? 'rgba(15,23,42,0.95)' : 'rgba(255,255,255,0.97)',
      titleColor: titleColor,
      bodyColor: textColor,
      borderColor: isDark ? 'rgba(255,255,255,0.12)' : 'rgba(0,0,0,0.1)',
      borderWidth: 1,
      padding: 10,
      cornerRadius: 8,
    };
  }

  function scaleOpts() {
    return {
      x: { grid: { color: gridColor }, ticks: { color: textColor, precision: 0 }, border: { color: gridColor } },
      y: { grid: { color: gridColor }, ticks: { color: textColor, precision: 0 }, border: { color: gridColor }, beginAtZero: true },
    };
  }

  var statusLabels = JSON.parse(el.dataset.statusLabels);
  var statusCounts = JSON.parse(el.dataset.statusCounts);
  var anosLabels   = JSON.parse(el.dataset.anosLabels);
  var anosCounts   = JSON.parse(el.dataset.anosCounts);
  var tipoLabels   = JSON.parse(el.dataset.tipoLabels);
  var tipoCounts   = JSON.parse(el.dataset.tipoCounts);
  var invLabels    = JSON.parse(el.dataset.investAnosLabels);
  var invValues    = JSON.parse(el.dataset.investAnosValues);
  var totalObras   = parseInt(el.dataset.total) || 1;

  var statusBgColors = statusLabels.map(corPorLabel);

  /* ── Gráfico: Status (barras horizontais) com rótulos dentro ── */
  _dashCharts.push(new Chart(document.getElementById('chart-status'), {
    type: 'bar',
    data: {
      labels: statusLabels,
      datasets: [{
        label: 'Obras',
        data: statusCounts,
        backgroundColor: statusBgColors.map(function(c) { return c + (isDark ? 'bb' : 'dd'); }),
        borderColor: statusBgColors,
        borderWidth: 1.5,
        borderRadius: 6,
        borderSkipped: false,
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: { display: false },
        tooltip: tooltipRico(totalObras),
        datalabels: false,
      },
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: textColor, precision: 0 }, border: { color: gridColor } },
        y: { grid: { display: false }, ticks: { color: textColor }, border: { display: false } },
      },
    },
  }));

  /* ── Gráfico: Obras por ano (linha com área) ── */
  _dashCharts.push(new Chart(document.getElementById('chart-anos'), {
    type: 'line',
    data: {
      labels: anosLabels,
      datasets: [{
        label: 'Obras',
        data: anosCounts,
        borderColor: '#3b7dd8',
        backgroundColor: isDark ? 'rgba(59,125,216,0.18)' : 'rgba(59,125,216,0.10)',
        tension: 0.4,
        fill: true,
        pointRadius: 5,
        pointBackgroundColor: '#3b7dd8',
        pointBorderColor: isDark ? '#1e293b' : '#fff',
        pointBorderWidth: 2,
        pointHoverRadius: 7,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: { display: false },
        tooltip: tooltipRico(totalObras),
      },
      scales: scaleOpts(),
    },
  }));

  /* ── Gráfico: Tipo de execução (doughnut) ── */
  var tipoBgColors = tipoLabels.map(function(_, i) { return CORES_PALETTE[i % CORES_PALETTE.length]; });
  _dashCharts.push(new Chart(document.getElementById('chart-tipo'), {
    type: 'doughnut',
    data: {
      labels: tipoLabels,
      datasets: [{
        data: tipoCounts,
        backgroundColor: tipoBgColors.map(function(c) { return c + 'cc'; }),
        borderColor: tipoBgColors,
        borderWidth: 2,
        hoverOffset: 6,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      cutout: '62%',
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: textColor, boxWidth: 10, padding: 10, font: { size: 11 } }
        },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              var total = ctx.dataset.data.reduce(function(a, b) { return a + b; }, 0);
              var pct = total > 0 ? ((ctx.parsed / total) * 100).toFixed(1) : 0;
              return ' ' + ctx.label + ': ' + ctx.parsed + ' (' + pct + '%)';
            }
          },
          backgroundColor: isDark ? 'rgba(15,23,42,0.95)' : 'rgba(255,255,255,0.97)',
          titleColor: titleColor,
          bodyColor: textColor,
          borderColor: isDark ? 'rgba(255,255,255,0.12)' : 'rgba(0,0,0,0.1)',
          borderWidth: 1,
          padding: 10,
          cornerRadius: 8,
        },
      },
    },
  }));

  /* ── Gráfico: Investimento por ano (barras verticais) ── */
  _dashCharts.push(new Chart(document.getElementById('chart-invest-ano'), {
    type: 'bar',
    data: {
      labels: invLabels,
      datasets: [{
        label: 'R$ mi',
        data: invValues,
        backgroundColor: isDark ? 'rgba(124,58,237,0.70)' : 'rgba(59,125,216,0.75)',
        borderColor:     isDark ? '#7c3aed' : '#3b7dd8',
        borderWidth: 1.5,
        borderRadius: 6,
        borderSkipped: false,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              return ' R$ ' + ctx.parsed.y.toLocaleString('pt-BR', { minimumFractionDigits: 2 }) + ' mi';
            }
          },
          backgroundColor: isDark ? 'rgba(15,23,42,0.95)' : 'rgba(255,255,255,0.97)',
          titleColor: titleColor,
          bodyColor: textColor,
          borderColor: isDark ? 'rgba(255,255,255,0.12)' : 'rgba(0,0,0,0.1)',
          borderWidth: 1,
          padding: 10,
          cornerRadius: 8,
        },
      },
      scales: scaleOpts(),
    },
  }));

  /* ── Animação de contagem nos cards ── */
  function animarContagem(el, alvo, duracao) {
    var inicio = performance.now();
    var ehNumero = !isNaN(parseInt(alvo));
    if (!ehNumero) { el.textContent = alvo; return; }
    alvo = parseInt(alvo);
    function step(agora) {
      var progresso = Math.min((agora - inicio) / duracao, 1);
      var ease = 1 - Math.pow(1 - progresso, 3);
      el.textContent = Math.round(alvo * ease);
      if (progresso < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  document.querySelectorAll('[data-count]').forEach(function(el) {
    animarContagem(el, el.dataset.count, 900);
  });

  /* Investimento total formatado */
  var investEl = document.querySelector('[data-invest]');
  if (investEl) {
    var valorInvest = parseFloat(investEl.dataset.invest) || 0;
    var inicio = performance.now();
    (function step(agora) {
      var p = Math.min((agora - inicio) / 900, 1);
      var ease = 1 - Math.pow(1 - p, 3);
      var val = valorInvest * ease;
      investEl.textContent = 'R$ ' + val.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      if (p < 1) requestAnimationFrame(step);
    })(performance.now());
  }

  /* ── Barras de progresso ── */
  requestAnimationFrame(function() {
    document.querySelectorAll('.dash-progress-fill').forEach(function(bar) {
      var pct;
      if (bar.dataset.fill) {
        pct = parseFloat(bar.dataset.fill);
      } else {
        var of    = parseFloat(bar.dataset.fillOf)    || 0;
        var total = parseFloat(bar.dataset.fillTotal) || 1;
        pct = Math.round((of / total) * 100);
      }
      bar.style.width = Math.min(pct, 100) + '%';
    });
  });
}

/* ── Modal Galeria ── */
function initModalGaleria() {
  var overlay = document.getElementById('modal-galeria-overlay');
  if (!overlay) return;

  function _fecharComEscape(e) {
    if (e.key === 'Escape') fecharModalGaleria();
  }

  function fecharModalGaleria() {
    document.removeEventListener('keydown', _fecharComEscape);
    var container = document.getElementById('container-modal-galeria');
    if (container) container.innerHTML = '';
  }

  var btnFechar = document.getElementById('btn-fechar-galeria');
  if (btnFechar) btnFechar.addEventListener('click', fecharModalGaleria);

  overlay.addEventListener('click', function (e) {
    if (e.target === this) fecharModalGaleria();
  });

  /* { once: true } garante no máximo um listener ativo por vez,
     mesmo se HTMX reinjectar o fragmento sem fechar o modal anterior. */
  document.addEventListener('keydown', _fecharComEscape, { once: true });

  /* Delegação para as fotos — evita onclick inline em cada <img> */
  overlay.addEventListener('click', function (e) {
    var img = e.target.closest('img.foto-galeria');
    if (img) abrirLightbox(img.src);
  });

  function abrirLightbox(src) {
    /* Remove o listener do modal antes de abrir o lightbox, para que
       Escape feche apenas o lightbox — não o modal inteiro simultaneamente. */
    document.removeEventListener('keydown', _fecharComEscape);

    var lb = document.createElement('div');
    lb.id = 'lightbox-fullscreen';
    lb.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.95);z-index:10000;display:flex;align-items:center;justify-content:center;cursor:zoom-out;';

    var imgEl = document.createElement('img');
    imgEl.src = src;
    imgEl.style.cssText = 'max-width:90%;max-height:90vh;border-radius:6px;object-fit:contain;box-shadow:0 0 40px rgba(0,0,0,0.8);';
    lb.appendChild(imgEl);

    function _fecharLightbox() {
      document.removeEventListener('keydown', _escapeDoLightbox);
      lb.remove();
      /* Restaura o listener do modal depois que o lightbox fecha. */
      document.addEventListener('keydown', _fecharComEscape, { once: true });
    }

    function _escapeDoLightbox(e) {
      if (e.key === 'Escape') _fecharLightbox();
    }

    lb.addEventListener('click', _fecharLightbox);
    document.addEventListener('keydown', _escapeDoLightbox);
    document.body.appendChild(lb);
  }
}

/* ── Zona Administrativa ── */
function initZonaAdmin() {
  /* ── Modal de exclusão de obra ── */
  var modal    = document.getElementById('modal-exclusao');
  var inputId  = document.getElementById('modal-exclusao-input-id');
  var spanId   = document.getElementById('modal-exclusao-id');
  var spanTipo = document.getElementById('modal-exclusao-tipo');

  if (modal) {
    var formExcObra = document.getElementById('form-confirmar-exclusao');
    if (formExcObra) _configurarRetry(formExcObra, 'submit');

    function fecharModal() { modal.style.display = 'none'; if (inputId) inputId.value = ''; }
    document.querySelectorAll('.btn-excluir[data-id]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (inputId)  inputId.value        = btn.dataset.id;
        if (spanId)   spanId.textContent   = btn.dataset.id;
        if (spanTipo) spanTipo.textContent = btn.dataset.tipo !== '—' ? btn.dataset.tipo : '';
        modal.style.display = 'flex';
      });
    });
    var btnCancObra = document.getElementById('modal-exclusao-cancelar');
    var bdropObra   = document.getElementById('modal-exclusao-backdrop');
    if (btnCancObra) btnCancObra.addEventListener('click', fecharModal);
    if (bdropObra)   bdropObra.addEventListener('click', fecharModal);
  }

  /* ── Modal de alteração de cargo ── */
  var modalCargo = document.getElementById('modal-cargo');
  if (modalCargo) {
    var formCargo = document.getElementById('form-alterar-cargo');
    if (formCargo) _configurarRetry(formCargo, 'submit');

    function fecharCargo() { modalCargo.style.display = 'none'; }
    document.querySelectorAll('.btn-alterar-cargo').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var cargoNome   = document.getElementById('modal-cargo-nome');
        var cargoAtual  = document.getElementById('modal-cargo-atual');
        var cargoCpf    = document.getElementById('modal-cargo-cpf');
        var cargoSelect = document.getElementById('modal-cargo-select');
        var cur = btn.dataset.cargoAtual;
        if (cargoNome)   cargoNome.textContent  = btn.dataset.nome;
        if (cargoAtual)  cargoAtual.textContent = 'Cargo atual: ' + (cur === 'SUPERVISOR' ? 'Supervisor' : 'Funcionário Comum');
        if (cargoSelect) cargoSelect.value = cur === 'SUPERVISOR' ? 'COMUM' : 'SUPERVISOR';
        if (cargoCpf)    cargoCpf.value = btn.dataset.cpf;
        modalCargo.style.display = 'flex';
      });
    });
    var btnCancCargo = document.getElementById('modal-cargo-cancelar');
    var bdropCargo   = document.getElementById('modal-cargo-backdrop');
    if (btnCancCargo) btnCancCargo.addEventListener('click', fecharCargo);
    if (bdropCargo)   bdropCargo.addEventListener('click', fecharCargo);
  }

  /* ── Modal de exclusão de funcionário ── */
  var modalExcFunc = document.getElementById('modal-exclusao-func');
  if (modalExcFunc) {
    var formExcFunc = document.getElementById('form-confirmar-excfunc');
    if (formExcFunc) _configurarRetry(formExcFunc, 'submit');

    function fecharExcFunc() { modalExcFunc.style.display = 'none'; }
    document.querySelectorAll('[data-cpf][data-nome].btn-excluir').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var excNome = document.getElementById('modal-excfunc-nome');
        var excCpf  = document.getElementById('modal-excfunc-cpf');
        if (excNome) excNome.textContent = btn.dataset.nome;
        if (excCpf)  excCpf.value        = btn.dataset.cpf;
        modalExcFunc.style.display = 'flex';
      });
    });
    var excCanc  = document.getElementById('modal-excfunc-cancelar');
    var excBdrop = document.getElementById('modal-exclusao-func-backdrop');
    if (excCanc)  excCanc.addEventListener('click', fecharExcFunc);
    if (excBdrop) excBdrop.addEventListener('click', fecharExcFunc);
  }

  /* ── Escape fecha qualquer modal aberto e limpa estado interno ── */
  _addDocListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (modal && modal.style.display === 'flex' && typeof fecharModal === 'function') fecharModal();
    if (modalCargo && modalCargo.style.display === 'flex' && typeof fecharCargo === 'function') fecharCargo();
    if (modalExcFunc && modalExcFunc.style.display === 'flex' && typeof fecharExcFunc === 'function') fecharExcFunc();
  });

  /* ── Click-to-expand: tooltip flutuante para textos truncados ── */
  var _tooltip = null;
  function _fecharTooltip() {
    if (_tooltip) { _tooltip.remove(); _tooltip = null; }
  }
  document.querySelectorAll('.exclusao-marquee').forEach(function (el) {
    if (el.scrollWidth <= el.clientWidth) return;
    el.style.cursor = 'pointer';
    el.addEventListener('click', function (e) {
      e.stopPropagation();
      if (_tooltip) { _fecharTooltip(); return; }
      var rect = el.getBoundingClientRect();
      _tooltip = document.createElement('div');
      _tooltip.className = 'exclusao-tooltip';
      _tooltip.textContent = el.textContent.trim();
      _tooltip.style.cssText = 'position:fixed;z-index:9000;left:' + rect.left + 'px;top:' + (rect.bottom + 4) + 'px;max-width:' + Math.min(400, window.innerWidth - rect.left - 12) + 'px;';
      document.body.appendChild(_tooltip);
    });
  });
  _addDocListener('click', _fecharTooltip);
}

/* ── Dispatcher ── */
function initPartial() {
  _removerListenersGlobais();
  if (document.getElementById('form-login'))             initLogin();
  if (document.getElementById('cpf-busca-func'))         initBuscaFunc();
  if (document.getElementById('form-cadastro-obras'))    initCadastroObras();
  if (document.getElementById('form-cadastro-func'))     initCadastroFunc();
  if (document.getElementById('form-edita-obra'))        initEditaObra();
  if (document.getElementById('form-edita-func'))        initEditaFunc();
  if (document.getElementById('modal-galeria-overlay'))  initModalGaleria();
  if (document.getElementById('dashboard-dados'))        initDashboard();
  if (document.getElementById('modal-exclusao') ||
      document.getElementById('modal-cargo') ||
      document.getElementById('modal-exclusao-func'))    initZonaAdmin();
}

/* ── Link ativo na sidebar ── */
function marcarLinkAtivo() {
  var pathname = window.location.pathname;

  // Remove active de todos os links
  document.querySelectorAll('.sidebar-link').forEach(function (el) {
    el.classList.remove('active');
  });

  // Fecha todos os submenus (serão reabertos se necessário)
  document.querySelectorAll('.sidebar-submenu').forEach(function (sub) {
    sub.classList.remove('open');
  });
  document.querySelectorAll('button.sidebar-link[aria-expanded]').forEach(function (btn) {
    btn.setAttribute('aria-expanded', 'false');
  });

  // Encontra o link cujo hx-get bate com o pathname atual
  var linkAtivo = null;
  document.querySelectorAll('.sidebar-link[hx-get]').forEach(function (el) {
    var href = el.getAttribute('hx-get') || '';
    // Compara sem query string
    if (href && pathname.startsWith(href.split('?')[0])) {
      // Prefere match mais específico (mais longo)
      if (!linkAtivo || href.length > (linkAtivo.getAttribute('hx-get') || '').length) {
        linkAtivo = el;
      }
    }
  });

  if (!linkAtivo) return;

  linkAtivo.classList.add('active');

  // Se o link ativo está dentro de um submenu, abre o pai
  var submenu = linkAtivo.closest('.sidebar-submenu');
  if (submenu) {
    submenu.classList.add('open');
    var btnPai = submenu.previousElementSibling;
    if (btnPai && btnPai.tagName === 'BUTTON') {
      btnPai.setAttribute('aria-expanded', 'true');
    }
  }
}

document.addEventListener('DOMContentLoaded',        function () { initPartial(); marcarLinkAtivo(); });
document.addEventListener('htmx:afterSwap',          function () { initPartial(); marcarLinkAtivo(); });
document.addEventListener('htmx:pushedIntoHistory',  function () { marcarLinkAtivo(); });
