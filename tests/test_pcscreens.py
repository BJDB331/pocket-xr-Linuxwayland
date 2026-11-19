"""PC screens, the parts that need no PC and no Moonlight: addresses, the host's answers, the command line, the slots,
and Moonlight's settings file (a real file's shape: Qt's INI, as Moonlight-qt 6 writes it)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pocketxr import pchelper, pcscreens as P  # noqa: E402

CERT = r'@ByteArray(-----BEGIN CERTIFICATE-----\nMIIC2zCCAcOgAwIBAgIBADANBg\n-----END CERTIFICATE-----\n)'
CONF = "\n".join([
    "[General]",
    "certificate=@ByteArray(-----BEGIN CERTIFICATE-----\\nAAAA\\n-----END CERTIFICATE-----\\n)",
    "uniqueid=0123456789ABCDEF",
    "",
    "[hosts]",
    "1\\apps\\1\\appcollector=false",
    "1\\apps\\1\\name=Desktop",
    "1\\apps\\size=1",
    "1\\hostname=MIKE",
    "1\\localaddress=192.168.1.20",
    "1\\localport=47989",
    "1\\mac=@ByteArray(\\0\\x11\\\"3DU)",
    "1\\manualaddress=",
    "1\\manualport=0",
    "1\\nvidiasw=false",
    "1\\remoteaddress=",
    "1\\remoteport=0",
    "1\\srvcert=" + CERT,
    "1\\uuid=AAAA-MAIN",
    "2\\hostname=OTHER",
    "2\\srvcert=@ByteArray()",
    "2\\uuid=BBBB-OTHER",
    "size=2",
    "",
    "[streaming]",
    "width=1920",
    "",
])


def test_bitrate_as_android():
    assert P.bitrate_kbps(1920, 1080, 60) == 40_000
    assert P.bitrate_kbps(1920, 1080, 60, "sharp") == 64_000
    assert P.bitrate_kbps(1920, 1080, 60, "saver") == 20_000
    assert P.bitrate_kbps(1920, 1080, 120) == round(40_000 * 2 ** 0.6)
    assert P.bitrate_kbps(640, 360, 30) == 8_000  # the floor
    assert P.bitrate_kbps(3840, 2160, 120, "sharp") == 150_000  # the ceiling


def test_addresses():
    assert P.parse_address("192.168.1.20") == ("192.168.1.20", 47989)
    assert P.parse_address(" 192.168.1.20:48989 ") == ("192.168.1.20", 48989)
    assert P.parse_address("[fe80::1]:48989") == ("fe80::1", 48989)
    assert P.parse_address("fe80::1") == ("fe80::1", 47989)
    assert P.parse_address("pc.local") == ("pc.local", 47989)
    for bad in ("", "  ", ":48989", "pc:0", "pc:99999", "pc:x", "[]:1", "[fe80::1]48989"):
        assert P.parse_address(bad) is None, bad
    assert P.address("192.168.1.20", 47989) == "192.168.1.20:47989"
    assert P.address("fe80::1", 48989) == "[fe80::1]:48989"


def test_serverinfo():
    xml = ("<?xml version=\"1.0\" encoding=\"utf-8\"?><root status_code=\"200\"><hostname>MIKE screen 2</hostname>"
           "<appversion>7.1.431.-1</appversion><uniqueid>5D8C-2</uniqueid><HttpsPort>48984</HttpsPort>"
           "<mac>00:11:22:33:44:55</mac><VirtualDisplayCapable>true</VirtualDisplayCapable>"
           "<VirtualDisplayDriverReady>true</VirtualDisplayDriverReady><PairStatus>0</PairStatus></root>")
    info = P.parse_serverinfo(xml)
    assert info == {"hostname": "MIKE screen 2", "uuid": "5D8C-2", "mac": "00:11:22:33:44:55", "https_port": 48984,
                    "virtual_display": True}
    assert P.parse_serverinfo("<root><hostname>x</hostname></root>") is None
    assert P.parse_serverinfo(xml.replace("<VirtualDisplayDriverReady>true", "<VirtualDisplayDriverReady>false"))[
        "virtual_display"] is False


def test_avahi_lines():
    out = "\n".join([
        "+;wlan0;IPv4;MIKE;_nvstream._tcp;local",
        "=;wlan0;IPv4;MIKE;_nvstream._tcp;local;MIKE.local;192.168.1.20;47989;",
        "=;wlan0;IPv4;MIKE\\0402\\041;_nvstream._tcp;local;MIKE.local;192.168.1.20;48989;",
        "=;wlan0;IPv6;MIKE;_nvstream._tcp;local;MIKE.local;fe80::1;47989;",
        "=;eth0;IPv4;MIKE;_nvstream._tcp;local;MIKE.local;192.168.1.20;47989;",
        "=;wlan0;IPv4;Living\\032room;_nvstream._tcp;local;lr.local;192.168.1.30;bad;",
    ])
    assert P.parse_avahi(out) == [("MIKE", "192.168.1.20", 47989), ("MIKE(2)", "192.168.1.20", 48989)]


def test_avahi_escapes():
    out = "=;wlan0;IPv4;Living\\032room;_nvstream._tcp;local;lr.local;192.168.1.30;47989;"
    assert P.parse_avahi(out) == [("Living room", "192.168.1.30", 47989)]


def test_app_list():
    text = ("Establishing connection to PC...\nLoading app list...\n"
            "Name, ID, HDR Support, App Collection Game, Hidden, Direct Launch, Boxart URL\n"
            "\"Steam Big Picture\",1093255277,true,false,false,false,\"\"\n"
            "\"Desktop\",881448767,true,false,false,false,\"\"\n")
    assert P.parse_app_list(text) == ["Steam Big Picture", "Desktop"]
    assert P.pick_app(P.parse_app_list(text)) == "Desktop"
    assert P.pick_app(["Virtual Display", "Steam"]) == "Virtual Display"
    assert P.pick_app([]) == "Desktop"


def test_slots():
    assert P.next_slot(set(), 3) == 0
    assert P.next_slot({0}, 3) == 1
    assert P.next_slot({0, 2}, 3) == 1
    assert P.next_slot({1}, 3) == 0  # screen 1 closed, 2 open: screen 1 again
    assert P.next_slot({0, 1, 2}, 3) is None
    assert P.next_slot({0}, 1) is None  # a PC with one Apollo streams one screen
    assert P.next_slot(set(), 5) == 0 and P.next_slot({0, 1, 2}, 5) is None


def test_screen_order():
    hosts = [{"hostname": "MIKE screen 3", "port": 48000}, {"hostname": "MIKE", "port": 49000},
             {"hostname": "MIKE Screen 2", "port": 47000}, {"hostname": "OTHER", "port": 47500}]
    assert [h["port"] for h in sorted(hosts, key=P.screen_order)] == [47500, 49000, 47000, 48000]


def test_stream_command():
    args = P.stream_args("192.168.1.20:48989", "Desktop")
    assert args[:3] == ["stream", "192.168.1.20:48989", "Desktop"]
    opts = dict(zip(args[3::1], args[4::1]))
    assert opts["--resolution"] == "1920x1080" and opts["--fps"] == "60" and opts["--bitrate"] == "40000"
    assert opts["--display-mode"] == "windowed"
    assert "--absolute-mouse" in args and "--quit-after" in args


def test_conf_reads_hosts_and_pairing():
    c = P.MoonlightConf(CONF)
    hosts = c.hosts()
    assert sorted(hosts) == [1, 2]
    assert "apps\\1\\name" not in hosts[1]
    assert c.paired("AAAA-MAIN") and c.paired("aaaa-main")
    assert not c.paired("BBBB-OTHER")  # no certificate
    assert not c.paired("CCCC-NONE")
    assert P.MoonlightConf("").hosts() == {} and not P.MoonlightConf("").paired("x")


def test_conf_adopts_a_second_apollo():
    c = P.MoonlightConf(CONF)
    assert c.adopt("AAAA-MAIN", "AAAA-SCREEN2", "MIKE screen 2", "192.168.1.20", 48989)
    again = P.MoonlightConf(c.text())  # read back as Moonlight would
    assert again.paired("AAAA-SCREEN2") and again.paired("AAAA-MAIN")
    i, h = again.find("AAAA-SCREEN2")
    assert i == 3
    assert h["srvcert"] == CERT
    assert h["hostname"] == "MIKE screen 2"
    assert h["localaddress"] == "192.168.1.20" and h["localport"] == "48989"
    assert h["manualaddress"] == "192.168.1.20" and h["manualport"] == "48989"
    assert h["remoteport"] == "0"  # no remote address: left as it was
    assert h["mac"] == "@ByteArray(\\0\\x11\\\"3DU)"
    assert "apps\\size" not in h
    # the main host, the other PC, and the other sections untouched
    main = again.find("AAAA-MAIN")[1]
    assert main["localport"] == "47989" and main["hostname"] == "MIKE"
    assert again.find("BBBB-OTHER")[1]["srvcert"] == "@ByteArray()"
    text = again.text()
    assert "size=3" in text and "size=2" not in text.split("[hosts]")[1].split("[streaming]")[0]
    assert "[streaming]\nwidth=1920" in text and "uniqueid=0123456789ABCDEF" in text
    assert text.index("3\\uuid=") < text.index("[streaming]")
    # a third, then the same again: no new entry
    assert again.adopt("AAAA-MAIN", "AAAA-SCREEN3", "MIKE screen 3", "192.168.1.20", 49989)
    assert again.adopt("AAAA-MAIN", "AAAA-SCREEN3", "MIKE screen 3", "192.168.1.20", 49989)
    assert sorted(again.hosts()) == [1, 2, 3, 4]


def test_conf_adopt_updates_an_entry_moonlight_found_unpaired():
    c = P.MoonlightConf(CONF)
    assert c.adopt("AAAA-MAIN", "BBBB-OTHER", "MIKE screen 2", "192.168.1.20", 48989)
    again = P.MoonlightConf(c.text())
    assert sorted(again.hosts()) == [1, 2]
    assert again.paired("BBBB-OTHER")
    assert again.find("BBBB-OTHER")[1]["manualport"] == "48989"


def test_conf_adopt_needs_a_paired_source():
    c = P.MoonlightConf(CONF)
    assert not c.adopt("BBBB-OTHER", "X", "x", "h", 1)
    assert not c.adopt("NONE", "X", "x", "h", 1)
    assert c.text() == P.MoonlightConf(CONF).text()


def test_conf_quotes_names_that_need_it():
    c = P.MoonlightConf(CONF)
    c.adopt("AAAA-MAIN", "S2", 'Mike, "office"', "192.168.1.20", 48989)
    assert P.MoonlightConf(c.text()).find("S2")[1]["hostname"] == '"Mike, \\"office\\""'
    assert P._ini_unquote(P.MoonlightConf(c.text()).find("S2")[1]["hostname"]) == 'Mike, "office"'


def test_conf_reads_the_backup_moonlight_did_not_finish():
    text = CONF + "\n[hostsbackup]\n1\\uuid=ZZZ\n1\\srvcert=" + CERT + "\nsize=1\n"
    c = P.MoonlightConf(text)
    assert c.paired("ZZZ") and c.find("AAAA-MAIN") is None


def test_conf_forget():
    c = P.MoonlightConf(CONF)
    assert c.forget("AAAA-MAIN")
    assert not P.MoonlightConf(c.text()).paired("AAAA-MAIN")
    assert not c.forget("NONE")


def test_conf_with_no_hosts_yet():
    c = P.MoonlightConf("[General]\nuniqueid=1\n")
    assert c.hosts() == {} and not c.adopt("A", "B", "b", "h", 1)


def test_helper_answer():
    assert pchelper.parse_here(b"POCKETXR-HERE 1 47610 MIKE PC") == (47610, "MIKE PC")
    assert pchelper.parse_here(b"POCKETXR-HERE 2 47610 MIKE") is None
    assert pchelper.parse_here(b"POCKETXR-HERE 1 x MIKE") is None
    assert pchelper.parse_here(b"hello") is None


def test_helper_token_file(tmp_path):
    path = str(tmp_path / "sub" / "pc-helper.json")
    assert pchelper.load_token(path) is None
    pchelper.save_token("abc", "MIKE", path)
    assert pchelper.load_token(path) == "abc"
    pchelper.forget_token(path)
    assert pchelper.load_token(path) is None
