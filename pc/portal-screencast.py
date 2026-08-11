#!/usr/bin/env python3
"""Pede ao portal do Wayland um nó PipeWire de captura de tela e imprime o node-id."""
import sys, random
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.ScreenCast"

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
sender = bus.get_unique_name()[1:].replace(".", "_")
loop = GLib.MainLoop()
state = {}

def token():
    return "t%d" % random.randint(0, 2**31)

def call(method, params):
    return bus.call_sync(BUS, PATH, IFACE, method, params, None,
                         Gio.DBusCallFlags.NONE, -1, None)

def wait(handle, cb):
    def on_sig(conn, s, p, i, sig, args):
        code, results = args.unpack()
        if code != 0:
            print(f"ERRO: portal retornou codigo {code} em {handle}", file=sys.stderr)
            loop.quit(); return
        cb(results)
    bus.signal_subscribe(BUS, "org.freedesktop.portal.Request", "Response",
                         handle, None, Gio.DBusSignalFlags.NONE, on_sig)

def step_create():
    t, st = token(), token()
    h = f"/org/freedesktop/portal/desktop/request/{sender}/{t}"
    wait(h, step_select)
    call("CreateSession", GLib.Variant("(a{sv})", ({
        "handle_token": GLib.Variant("s", t),
        "session_handle_token": GLib.Variant("s", st)},)))

def step_select(res):
    state["session"] = res["session_handle"]
    t = token()
    h = f"/org/freedesktop/portal/desktop/request/{sender}/{t}"
    wait(h, step_start)
    call("SelectSources", GLib.Variant("(oa{sv})", (state["session"], {
        "handle_token": GLib.Variant("s", t),
        "types": GLib.Variant("u", 1),        # 1 = monitor
        "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", 2),  # 2 = embutir cursor
    })))

def step_start(res):
    t = token()
    h = f"/org/freedesktop/portal/desktop/request/{sender}/{t}"
    wait(h, step_done)
    call("Start", GLib.Variant("(osa{sv})", (state["session"], "", {
        "handle_token": GLib.Variant("s", t)})))

def step_done(res):
    streams = res.get("streams", [])
    if not streams:
        print("ERRO: nenhum stream retornado", file=sys.stderr); loop.quit(); return
    node_id, props = streams[0]
    size = props.get("size", ("?", "?"))
    print(f"NODE_ID={node_id}")
    print(f"SIZE={size[0]}x{size[1]}", file=sys.stderr)
    loop.quit()

step_create()
GLib.timeout_add_seconds(90, lambda: (print("ERRO: timeout esperando escolha", file=sys.stderr), loop.quit())[1])
loop.run()
