"""AIS NMEA decoder tests on public sample sentences (gpsd regression data). No network."""

import unittest
from pathlib import Path

from meridian.modules.ships import ais

FIXTURES = Path(__file__).parent / "fixtures" / "ships"


def sample_lines():
    return [ln for ln in (FIXTURES / "nmea_samples.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


class SentenceLayer(unittest.TestCase):
    def test_checksum(self):
        self.assertEqual(ais.nmea_checksum("AIVDM,1,1,,A,15RTgt0PAso;90TKcjM8h6g208CQ,0"), 0x4A)

    def test_bad_checksum_is_dropped(self):
        self.assertIsNone(ais.parse_sentence("!AIVDM,1,1,,A,15RTgt0PAso;90TKcjM8h6g208CQ,0*4B"))
        asm = ais.Assembler()
        self.assertIsNone(asm.feed("!AIVDM,1,1,,A,15RTgt0PAso;90TKcjM8h6g208CQ,0*4B"))
        self.assertEqual(asm.stats["bad"], 1)

    def test_rejects_garbage(self):
        for line in ("", "hello", "$GPGGA,1,2,3*00", "!AIVDM,1,1,,A", "!AIVDM,x,1,,A,1,0*00"):
            self.assertIsNone(ais.parse_sentence(line), line)

    def test_tag_block_time(self):
        s = ais.parse_sentence("\\g:1-2-73874,n:157036,s:r003669945,c:1241544035*4A\\"
                               "!AIVDM,1,1,,B,15N4cJ`005Jrek0H@9n`DW5608EP,0*13")
        self.assertIsNotNone(s)
        self.assertEqual(s.tag_time, 1241544035.0)
        ms = ais.parse_sentence("\\c:1241544035000*00\\!AIVDM,1,1,,B,15N4cJ`005Jrek0H@9n`DW5608EP,0*13")
        self.assertEqual(ms.tag_time, 1241544035.0)

    def test_uscg_trailing_fields(self):
        s = ais.parse_sentence("!AIVDM,1,1,,B,15Cjtd0Oj;Jp7ilG7=UkKBoB0<06,0*63,s1234,d-119,T12.34567123,"
                               "r003669958,1085889680")
        self.assertIsNotNone(s)
        self.assertEqual(s.payload, "15Cjtd0Oj;Jp7ilG7=UkKBoB0<06")

    def test_other_talkers_and_vdo(self):
        line = "!AIVDM,1,1,,A,15RTgt0PAso;90TKcjM8h6g208CQ,0"
        body = "BSVDM" + line[6:]
        s = ais.parse_sentence(f"!{body}*{ais.nmea_checksum(body):02X}")
        self.assertEqual(s.talker, "BSVDM")
        body = "AIVDO" + line[6:]
        s = ais.parse_sentence(f"!{body}*{ais.nmea_checksum(body):02X}")
        self.assertTrue(s.own)


class PayloadArmour(unittest.TestCase):
    def test_six_bit_values(self):
        b = ais.Bits("0Ww")       # 0 -> 0, W -> 39, w -> 63
        self.assertEqual(b.n, 18)
        self.assertEqual(b.uint(0, 6), 0)
        self.assertEqual(b.uint(6, 6), 39)
        self.assertEqual(b.uint(12, 6), 63)

    def test_fill_bits(self):
        b = ais.Bits("w", 2)
        self.assertEqual(b.n, 4)
        self.assertEqual(b.uint(0, 4), 15)

    def test_signed(self):
        b = ais.Bits("w")         # 111111 -> -1 as a 6-bit signed number
        self.assertEqual(b.int(0, 6), -1)

    def test_bad_character(self):
        with self.assertRaises(ValueError):
            ais.Bits("~")
        self.assertIsNone(ais.decode_payload("~~~~"))

    def test_text(self):
        # "@" padding is stripped; values below 32 map to "@".."_"
        b = ais.Bits("H42O55i18tMET00000000000000", 2)
        self.assertEqual(b.text(40, 120), "PROGUY")


class MessageTypes(unittest.TestCase):
    def setUp(self):
        asm = ais.Assembler()
        self.msgs = [m for m in (asm.feed(line, now=1000.0) for line in sample_lines()) if m]
        self.asm = asm

    def find(self, mmsi, mtype):
        return next(m for m in self.msgs if m["mmsi"] == mmsi and m["type"] == mtype)

    def test_counts(self):
        self.assertEqual(len(self.msgs), 14)
        self.assertEqual(self.asm.stats["bad"], 0)

    def test_type1(self):
        m = self.find(371798000, 1)
        self.assertEqual(m["cls"], "A")
        self.assertEqual(m["nav_status"], 0)
        self.assertAlmostEqual(m["sog"], 12.3)
        self.assertAlmostEqual(m["lon"], -123.395383, places=5)
        self.assertAlmostEqual(m["lat"], 48.381633, places=5)
        self.assertAlmostEqual(m["cog"], 224.0)
        self.assertEqual(m["heading"], 215)
        self.assertEqual(m["second"], 33)
        self.assertTrue(m["accuracy"])
        self.assertEqual(m["rot"], -720.0)       # -127: turning left, no turn indicator

    def test_type1_unavailable_heading(self):
        m = self.find(440348000, 1)
        self.assertIsNone(m["heading"])          # 511
        self.assertIsNone(m["rot"])              # -128
        self.assertAlmostEqual(m["cog"], 93.4)
        self.assertAlmostEqual(m["lat"], 43.08015, places=5)

    def test_type2_and_3(self):
        m2 = self.find(356302000, 2)
        self.assertAlmostEqual(m2["sog"], 13.9)
        self.assertAlmostEqual(m2["lon"], -71.626143, places=5)
        m3 = self.find(563808000, 3)
        self.assertEqual(m3["nav_status"], 5)
        self.assertAlmostEqual(m3["lat"], 36.91, places=5)
        self.assertEqual(m3["heading"], 352)

    def test_type5_multipart(self):
        m = self.find(351759000, 5)
        self.assertEqual(m["imo"], 9134270)
        self.assertEqual(m["callsign"], "3FOF8")
        self.assertEqual(m["name"], "EVER DIADEM")
        self.assertEqual(m["ship_type"], 70)
        self.assertEqual((m["dim_a"], m["dim_b"], m["dim_c"], m["dim_d"]), (225, 70, 1, 31))
        self.assertEqual(ais.dimensions(m), (295.0, 32.0))
        self.assertAlmostEqual(m["draught"], 12.2)
        self.assertEqual(m["destination"], "NEW YORK")
        self.assertEqual(m["eta"], "05-15T14:00Z")

    def test_channel_multiplexing(self):
        both = [m for m in self.msgs if m["mmsi"] == 271010059]
        self.assertEqual(len(both), 2)
        for m in both:
            self.assertEqual(m["name"], "HEALTH CONTROL 13")
            self.assertEqual(m["callsign"], "TCA2350")
            self.assertEqual(m["ship_type"], 55)
            self.assertAlmostEqual(m["draught"], 2.0)
            self.assertIsNone(m["eta"])           # 00-00T24:60 means not available
            self.assertIsNone(m["imo"])

    def test_type18(self):
        m = self.find(338087471, 18)
        self.assertEqual(m["cls"], "B")
        self.assertAlmostEqual(m["sog"], 0.1)
        self.assertAlmostEqual(m["lon"], -74.072132, places=5)
        self.assertAlmostEqual(m["lat"], 40.68454, places=5)
        self.assertAlmostEqual(m["cog"], 79.6)
        self.assertIsNone(m["heading"])
        m2 = self.find(338088483, 18)
        self.assertAlmostEqual(m2["cog"], 171.6)

    def test_type19(self):
        m = self.find(367059850, 19)
        self.assertEqual(m["name"], "CAPT.J.RIMES")
        self.assertEqual(m["ship_type"], 70)
        self.assertAlmostEqual(m["sog"], 8.7)
        self.assertAlmostEqual(m["cog"], 335.9)
        self.assertAlmostEqual(m["lat"], 29.543695, places=5)
        self.assertEqual(ais.dimensions(m), (26.0, 8.0))

    def test_type24_parts(self):
        a = next(m for m in self.msgs if m["type"] == 24 and m["part"] == 0)
        b = next(m for m in self.msgs if m["type"] == 24 and m["part"] == 1)
        self.assertEqual(a["mmsi"], 271041815)
        self.assertEqual(a["name"], "PROGUY")
        self.assertEqual(b["ship_type"], 60)
        self.assertEqual(b["callsign"], "TC6163")
        self.assertEqual(ais.dimensions(b), (15.0, 5.0))

    def test_tag_time_carried(self):
        m = self.find(367078250, 1)
        self.assertEqual(m["tag_time"], 1241544035.0)
        self.assertEqual(m["nav_status"], 8)

    def test_out_of_order_fragment_dropped(self):
        asm = ais.Assembler()
        self.assertIsNone(asm.feed("!AIVDM,2,2,1,A,88888888880,2*25"))
        self.assertEqual(asm.stats["bad"], 1)

    def test_unsupported_type(self):
        asm = ais.Assembler()
        self.assertIsNone(asm.feed("!AIVDM,1,1,,A,403OviQuMGCqWrRO9>E6fE700@GO,0*4D"))   # type 4 base station
        self.assertEqual(asm.stats["unsupported"], 1)


class Helpers(unittest.TestCase):
    def test_imo_checksum(self):
        self.assertTrue(ais.valid_imo(9134270))
        self.assertTrue(ais.valid_imo("9074729"))
        self.assertFalse(ais.valid_imo(9134271))
        self.assertFalse(ais.valid_imo(1234568))
        self.assertFalse(ais.valid_imo(None))
        self.assertFalse(ais.valid_imo("abc"))

    def test_eta(self):
        self.assertEqual(ais.eta_from_packed(772510), "11-25T06:30Z")
        self.assertIsNone(ais.eta_from_packed(0))
        self.assertEqual(ais.eta_text(5, 15, 24, 60), "05-15")
        now = 1795380000.0   # 2026-11-22
        t = ais.eta_to_epoch("11-25T06:30Z", now)
        self.assertEqual(t, 1795588200.0)                 # 2026-11-25 06:30 UTC
        past = ais.eta_to_epoch("10-17T14:00Z", now)       # five weeks ago: stays in the past
        self.assertLess(past, now)
        jan = ais.eta_to_epoch("01-05T00:00Z", now)        # early next year
        self.assertGreater(jan, now)
        self.assertIsNone(ais.eta_to_epoch("13-01T00:00Z", now))

    def test_clean_text(self):
        self.assertEqual(ais.clean_text("TEST  CARGO@@@ "), "TEST CARGO")
        self.assertEqual(ais.clean_text(None), "")

    def test_rot(self):
        self.assertIsNone(ais._rot(-128))
        self.assertEqual(ais._rot(0), 0.0)
        self.assertAlmostEqual(ais._rot(30), 40.2, places=1)
        self.assertLess(ais._rot(-30), 0)


if __name__ == "__main__":
    unittest.main()
