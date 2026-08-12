#!/usr/bin/env python3
"""Interface para usar a TV pela caixinha RK3229.

Dois modos:
  - Espelhar a tela do PC (tv-cast.py -> RTP -> box)
  - Tocar um link direto na TV (tv-player no box, sem passar pelo PC)
"""
import json
import os
import shutil
import signal
import threading
import socket
import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

CAST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tv-cast.py")
# Endereco do box: variavel de ambiente, ou ~/.config/tv-cast.conf, ou o padrao.
# Nao deixar fixo no fonte — quem clonar o repositorio tem outro IP.
def _conf(chave, padrao):
    v = os.environ.get(chave)
    if v:
        return v
    caminho = os.path.expanduser("~/.config/tv-cast.conf")
    if os.path.exists(caminho):
        for linha in open(caminho):
            if linha.strip().startswith(chave + "="):
                return linha.split("=", 1)[1].strip()
    return padrao


BOX_HOST = _conf("TV_BOX_HOST", "192.168.10.159")
BOX_USER = _conf("TV_BOX_USER", "reinoldo")
BOX_PORTA_PLAYER = int(_conf("TV_BOX_PORT", "5010"))


def token_de(origem):
    """O portal guarda a escolha por tipo de fonte — monitor e janela sao tokens
    diferentes, senao escolher uma tela apagaria a janela salva."""
    return os.path.expanduser(f"~/.cache/tv-cast-restore-token-{origem}")


ORIGENS = [("Uma tela inteira", "monitor"), ("Apenas uma janela", "janela")]

# So faz sentido com uma janela: numa tela inteira o som e sempre o do sistema.
SONS = [("Só do programa da janela", "janela"), ("Tudo que sai no computador", "sistema")]

# Idem: esticar uma janela pra preencher a TV custa nitidez (medido: 96,75 contra 99,60
# de VMAF, e 68 contra 88 nos piores quadros). Tela inteira sempre precisa ser reduzida.
TAMANHOS = [("Tamanho real, com borda preta", "tamanho-real"),
            ("Esticar até preencher a TV", "preencher")]

# (rotulo, kbps) — taxas escolhidas por medicao de SSIM contra o original:
#   8 Mbps -> ~0.985 | 20 Mbps -> 0.9983 | 30 Mbps -> 0.9994 (quase transparente)
# O enlace da 95 Mbit/s e o box decodifica ate 40 Mbps a 60fps sem perder quadro.
QUALIDADES = [("Econômica — 8 Mbps", 8000),
              ("Alta — 20 Mbps", 20000),
              ("Máxima — 30 Mbps", 30000)]
PADRAO = 1


def notificar(titulo, corpo):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "TV", titulo, corpo],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def box_cmd(timeout=6, **kw):
    """Fala com o tv-player do box (JSON por linha, TCP 5010)."""
    try:
        s = socket.create_connection((BOX_HOST, BOX_PORTA_PLAYER), timeout=timeout)
        s.sendall((json.dumps(kw) + "\n").encode())
        resp = s.makefile("r").readline()
        s.close()
        return json.loads(resp or "{}")
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def mmss(seg):
    seg = int(max(0, seg))
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Janela(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="TV")
        self.set_default_size(440, -1)
        self.proc = self.watch = None
        self.poll = None

        raiz = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(raiz)
        cab = Adw.HeaderBar()
        raiz.append(cab)
        menu = Gio.Menu()
        menu.append("Sobre", "win.sobre")
        cab.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        corpo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                        margin_top=20, margin_bottom=20, margin_start=18, margin_end=18)
        raiz.append(corpo)

        # ---------------------------------------------------------- estado
        self.icone = Gtk.Image(icon_name="video-display-symbolic", pixel_size=44)
        self.icone.add_css_class("dim-label")
        corpo.append(self.icone)
        self.titulo = Gtk.Label(label="Pronto")
        self.titulo.add_css_class("title-2")
        self.titulo.set_wrap(True)
        self.titulo.set_justify(Gtk.Justification.CENTER)
        corpo.append(self.titulo)
        self.detalhe = Gtk.Label(label="A TV está livre")
        self.detalhe.add_css_class("dim-label")
        self.detalhe.set_wrap(True)
        self.detalhe.set_justify(Gtk.Justification.CENTER)
        corpo.append(self.detalhe)

        # ------------------------------------------------ tocar na TV
        g_link = Adw.PreferencesGroup(
            title="Tocar na TV",
            description="A caixinha baixa e reproduz sozinha, sem recodificar — "
                        "o computador pode até ser desligado")
        corpo.append(g_link)

        self.link = Adw.EntryRow(title="Link do vídeo")
        self.link.connect("entry-activated", lambda *_: self.tocar())
        g_link.add(self.link)

        linha_b = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6,
                          margin_bottom=6)
        b_tocar = Gtk.Button(label="Tocar agora")
        b_tocar.add_css_class("suggested-action")
        b_tocar.add_css_class("pill")
        b_tocar.connect("clicked", lambda *_: self.tocar())
        b_fila = Gtk.Button(label="Adicionar à fila")
        b_fila.add_css_class("pill")
        b_fila.connect("clicked", lambda *_: self.tocar(fila=True))
        linha_b.append(b_tocar)
        linha_b.append(b_fila)
        g_link.add(Adw.ActionRow(child=linha_b, activatable=False))

        # ---- controles de midia (aparecem so quando ha algo tocando)
        self.painel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                              margin_top=6, margin_bottom=6)
        barra = Gtk.Box(spacing=8)
        self.t_pos = Gtk.Label(label="0:00")
        self.t_pos.add_css_class("numeric")
        self.t_pos.add_css_class("dim-label")
        self.progresso = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
        self.t_dur = Gtk.Label(label="0:00")
        self.t_dur.add_css_class("numeric")
        self.t_dur.add_css_class("dim-label")
        barra.append(self.t_pos)
        barra.append(self.progresso)
        barra.append(self.t_dur)
        self.painel.append(barra)

        botoes = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        def bt(icone, dica, fn, css="flat"):
            b = Gtk.Button(icon_name=icone, tooltip_text=dica)
            b.add_css_class(css)
            b.connect("clicked", fn)
            botoes.append(b)
            return b
        self.b_prev = bt("media-skip-backward-symbolic", "Anterior da fila",
                         lambda *_: self.enviar(cmd="prev"))
        self.b_play = bt("media-playback-pause-symbolic", "Pausar ou continuar",
                         lambda *_: self.enviar(cmd="toggle"), "circular")
        self.b_next = bt("media-skip-forward-symbolic", "Próximo da fila",
                         lambda *_: self.enviar(cmd="next"))
        bt("media-playback-stop-symbolic", "Parar e liberar a TV",
           lambda *_: self.parar_tv())
        self.painel.append(botoes)

        self.aviso_busca = Gtk.Label(
            label="Não é possível avançar dentro do vídeo em 1080p — "
                  "o YouTube entrega essa qualidade em fragmentos que não permitem busca")
        self.aviso_busca.add_css_class("dim-label")
        self.aviso_busca.add_css_class("caption")
        self.aviso_busca.set_wrap(True)
        self.aviso_busca.set_justify(Gtk.Justification.CENTER)
        self.painel.append(self.aviso_busca)

        self.fila_rotulo = Gtk.Label(label="")
        self.fila_rotulo.add_css_class("dim-label")
        self.fila_rotulo.add_css_class("caption")
        self.fila_rotulo.set_wrap(True)
        self.painel.append(self.fila_rotulo)

        g_link.add(Adw.ActionRow(child=self.painel, activatable=False))
        self.painel.set_visible(False)

        # ------------------------------------------------ espelhar a tela
        g_esp = Adw.PreferencesGroup(title="Espelhar a tela do computador")
        corpo.append(g_esp)

        self.origem = Adw.ComboRow(title="O que transmitir")
        self.origem.set_model(Gtk.StringList.new([o[0] for o in ORIGENS]))
        self.origem.connect("notify::selected", self.ao_trocar_origem)
        g_esp.add(self.origem)

        self.escolha = Adw.ActionRow(title="Escolher outra")
        b_troca = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Perguntar de novo qual tela ou janela usar")
        b_troca.add_css_class("flat")
        b_troca.connect("clicked", self.trocar_tela)
        self.escolha.add_suffix(b_troca)
        g_esp.add(self.escolha)

        self.combo = Adw.ComboRow(title="Qualidade")
        self.combo.set_model(Gtk.StringList.new([q[0] for q in QUALIDADES]))
        self.combo.set_selected(PADRAO)
        g_esp.add(self.combo)

        self.tamanho = Adw.ComboRow(title="Como mostrar na TV")
        self.tamanho.set_model(Gtk.StringList.new([t[0] for t in TAMANHOS]))
        self.tamanho.connect("notify::selected", self.ao_trocar_origem)
        g_esp.add(self.tamanho)

        self.audio = Adw.SwitchRow(title="Enviar o som")
        self.audio.set_active(True)
        self.audio.connect("notify::active", self.ao_trocar_origem)
        g_esp.add(self.audio)

        self.som = Adw.ComboRow(title="De onde vem o som")
        self.som.set_model(Gtk.StringList.new([s[0] for s in SONS]))
        self.som.connect("notify::selected", self.ao_trocar_origem)
        g_esp.add(self.som)
        self.ao_trocar_origem()

        self.botao = Gtk.Button(label="Espelhar")
        self.botao.add_css_class("pill")
        self.botao.set_halign(Gtk.Align.CENTER)
        self.botao.connect("clicked", self.alternar)
        corpo.append(self.botao)

        a = Gio.SimpleAction.new("sobre", None)
        a.connect("activate", self.mostrar_sobre)
        self.add_action(a)
        self.connect("close-request", self.ao_fechar)
        self.iniciar_poll()

    # ------------------------------------------------------------ tocar na TV
    def enviar(self, **kw):
        r = box_cmd(**kw)
        if not r.get("ok") and r.get("erro"):
            self.detalhe.set_text(str(r["erro"]))
        self.atualizar()
        return r

    def tocar(self, fila=False):
        url = self.link.get_text().strip()
        if not url:
            self.detalhe.set_text("Cole um link primeiro")
            return
        if self.espelhando():
            self.parar_espelho()   # espelhar e tocar disputam a mesma TV
        self.titulo.set_text("Resolvendo o link...")
        self.detalhe.set_text("A caixinha está buscando o vídeo, leva alguns segundos")
        self.link.set_text("")

        # Em thread: o yt-dlp leva dezenas de segundos no Cortex-A7 e chamar isso
        # na thread da interface congelaria a janela inteira.
        def trabalho():
            r = box_cmd(timeout=180, cmd="add" if fila else "play", url=url)
            GLib.idle_add(self.ao_resolver, r)
        threading.Thread(target=trabalho, daemon=True).start()

    def ao_resolver(self, r):
        if r.get("ok"):
            notificar("Tocando na TV", r.get("titulo", ""))
        else:
            self.detalhe.set_text(r.get("erro", "não consegui tocar"))
        self.atualizar()
        return False

    def parar_tv(self):
        self.enviar(cmd="stop")
        notificar("Reprodução encerrada", "A TV voltou ao modo de espelhamento")

    def iniciar_poll(self):
        if self.poll is None:
            self.poll = GLib.timeout_add_seconds(1, self.atualizar)

    def atualizar(self, *_):
        if self.espelhando():
            return True
        st = box_cmd(timeout=3, cmd="status")
        if not st.get("ok"):
            # falha de consulta e transitoria (box ocupado, rede) — nao alarmar
            return True
        if st.get("estado") == "resolvendo":
            self.painel.set_visible(False)
            self.titulo.set_text("Resolvendo o link...")
            return True
        tocando = st.get("estado") in ("tocando", "pausado")
        self.painel.set_visible(bool(tocando))
        if not tocando:
            self.pintar_ocioso()
            return True

        pos, dur = st.get("pos", 0), st.get("dur", 0)
        vivo = bool(st.get("vivo"))
        # Ao vivo nao tem duracao nem faz sentido avancar/voltar na fila.
        self.progresso.set_visible(not vivo)
        self.t_dur.set_visible(not vivo)
        self.b_prev.set_visible(not vivo)
        self.b_next.set_visible(not vivo)
        self.aviso_busca.set_visible(not vivo)
        if vivo:
            self.t_pos.set_text("● AO VIVO")
            self.t_pos.remove_css_class("dim-label")
            self.t_pos.add_css_class("error")
        else:
            self.t_pos.remove_css_class("error")
            self.t_pos.add_css_class("dim-label")
            self.progresso.set_fraction(min(1.0, pos / dur) if dur else 0.0)
            self.t_pos.set_text(mmss(pos))
            self.t_dur.set_text(mmss(dur))
        pausado = st.get("estado") == "pausado"
        self.b_play.set_icon_name("media-playback-start-symbolic" if pausado
                                  else "media-playback-pause-symbolic")
        self.titulo.set_text(st.get("titulo") or "Tocando na TV")
        self.detalhe.set_text("Pausado" if pausado else "Tocando direto na caixinha")
        self.icone.set_from_icon_name("media-playback-start-symbolic")
        self.icone.remove_css_class("dim-label")
        self.icone.add_css_class("success")
        f = st.get("fila") or []
        self.fila_rotulo.set_text(
            f"Fila: {st.get('idx', 0) + 1} de {len(f)}" if len(f) > 1 else "")
        return True

    def pintar_ocioso(self):
        self.titulo.set_text("Pronto")
        self.detalhe.set_text("A TV está livre")
        self.icone.set_from_icon_name("video-display-symbolic")
        self.icone.remove_css_class("success")
        self.icone.add_css_class("dim-label")

    # ------------------------------------------------------------ espelhar
    def espelhando(self):
        return self.proc is not None and self.proc.poll() is None

    def origem_atual(self):
        return ORIGENS[self.origem.get_selected()][1]

    def ao_trocar_origem(self, *_):
        o = self.origem_atual()
        alvo = "janela" if o == "janela" else "tela"
        self.escolha.set_subtitle(f"Uma {alvo} já está memorizada"
                                  if os.path.exists(token_de(o))
                                  else f"Vai perguntar qual {alvo} usar")
        self.tamanho.set_visible(o == "janela")
        self.tamanho.set_subtitle(
            "Mais nítido: os pixels da janela vão sem esticar"
            if TAMANHOS[self.tamanho.get_selected()][1] == "tamanho-real" else
            "Ocupa a TV inteira, ao custo de borrar um pouco o texto")
        self.som.set_visible(o == "janela" and self.audio.get_active())
        self.som.set_subtitle(
            "Abas de um mesmo navegador contam como um programa só"
            if SONS[self.som.get_selected()][1] == "janela" else
            "Inclui notificações, chamadas e qualquer outro programa")

    def alternar(self, _b):
        self.parar_espelho() if self.espelhando() else self.iniciar_espelho()

    def iniciar_espelho(self):
        o = self.origem_atual()
        cmd = [sys.executable, CAST, "--bitrate",
               str(QUALIDADES[self.combo.get_selected()][1]), "--source", o]
        if o == "janela":
            cmd += ["--escala", TAMANHOS[self.tamanho.get_selected()][1]]
        if not self.audio.get_active():
            cmd.append("--no-audio")
        elif o == "janela":
            cmd += ["--audio-source", SONS[self.som.get_selected()][1]]
        box_cmd(cmd="stop")   # libera a TV se estiver tocando algo
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         start_new_session=True)
        except Exception as e:
            self.detalhe.set_text(f"Não foi possível iniciar: {e}")
            return
        self.titulo.set_text("Espelhando")
        self.detalhe.set_text("A TV está mostrando esta tela"
                              if os.path.exists(token_de(o))
                              else "Escolha no seletor que apareceu")
        self.icone.set_from_icon_name("display-symbolic")
        self.icone.remove_css_class("dim-label")
        self.icone.add_css_class("success")
        self.botao.set_label("Parar de espelhar")
        self.botao.add_css_class("destructive-action")
        self.painel.set_visible(False)
        self.watch = GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid,
                                          self.ao_encerrar_espelho)
        notificar("Espelhando na TV", "")

    def parar_espelho(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                self.proc.terminate()
        self.ao_encerrar_espelho()

    def ao_encerrar_espelho(self, *_):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = None
        self.proc = None
        self.botao.set_label("Espelhar")
        self.botao.remove_css_class("destructive-action")
        self.pintar_ocioso()
        return False

    def trocar_tela(self, *_):
        if self.espelhando():
            self.parar_espelho()
        try:
            os.remove(token_de(self.origem_atual()))
        except FileNotFoundError:
            pass
        self.ao_trocar_origem()

    def mostrar_sobre(self, *_):
        Adw.AboutWindow(
            transient_for=self, application_name="TV",
            developer_name="Caixinha RK3229 como receptor",
            version="2.0",
            comments=("Tocar um link: a caixinha baixa e reproduz sozinha, em qualidade "
                      "original e sem recodificar — o computador nem precisa estar ligado.\n\n"
                      "Espelhar: a placa de vídeo comprime esta tela e a caixinha "
                      "decodifica no chip de vídeo dela. Útil para serviços com proteção "
                      "de conteúdo, que só abrem no navegador do computador."),
        ).present()

    def ao_fechar(self, *_):
        if self.espelhando():
            self.parar_espelho()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.rk322x.TvCast")

    def do_activate(self):
        (self.props.active_window or Janela(self)).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
