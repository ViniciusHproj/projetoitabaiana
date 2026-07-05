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
}

/* ── Busca de Funcionário ── */
function initBuscaFunc() {
  var inputCPF = document.getElementById('cpf-busca-func');
  if (!inputCPF) return;
  inputCPF.addEventListener('input', function () { mascaraCPF(this); });
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

  function abrirModal() { modal.style.display = 'flex'; document.getElementById('modal-confirmar').focus(); }
  function fecharModal() { modal.style.display = 'none'; }

  document.getElementById('btn-limpar-obras').addEventListener('click', limparFormObras);
  document.getElementById('btn-salvar').addEventListener('click', abrirModal);
  document.getElementById('modal-confirmar').addEventListener('click', function () { fecharModal(); htmx.trigger(form, 'confirmar-cadastro'); });
  document.getElementById('modal-cancelar').addEventListener('click', fecharModal);
  document.getElementById('modal-backdrop').addEventListener('click', fecharModal);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

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
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

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

  function abrirModal() { modal.style.display = 'flex'; document.getElementById('modal-confirmar-edita-obra').focus(); }
  function fecharModal() { modal.style.display = 'none'; }

  document.getElementById('btn-cancelar-edita-obra').addEventListener('click', function () { window.location.reload(); });
  document.getElementById('btn-salvar-alteracoes').addEventListener('click', abrirModal);
  document.getElementById('btn-fechar-modal-edita-obra').addEventListener('click', fecharModal);
  document.getElementById('modal-confirmar-edita-obra').addEventListener('click', function () { fecharModal(); htmx.trigger(form, 'confirmar-edita-obra'); });
  document.getElementById('modal-backdrop-edita-obra').addEventListener('click', fecharModal);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

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
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && modal.style.display === 'flex') fecharModal(); });

  var rgInput = document.getElementById('rg-edita-func');
  if (rgInput) rgInput.addEventListener('input', function () { mascaraRG(this); });
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

/* ── Dispatcher ── */
function initPartial() {
  if (document.getElementById('form-login'))             initLogin();
  if (document.getElementById('cpf-busca-func'))         initBuscaFunc();
  if (document.getElementById('form-cadastro-obras'))    initCadastroObras();
  if (document.getElementById('form-cadastro-func'))     initCadastroFunc();
  if (document.getElementById('form-edita-obra'))        initEditaObra();
  if (document.getElementById('form-edita-func'))        initEditaFunc();
  if (document.getElementById('modal-galeria-overlay'))  initModalGaleria();
}

document.addEventListener('DOMContentLoaded', initPartial);
document.addEventListener('htmx:afterSwap',   initPartial);
