"""Demo scenarios. Merchants, prices and the event are fictional; currencies and categories are real."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from .clock import CLOCK
from .mandate import Cond, LegSpec, Mandate, Offer, Principal, Substitution

D = Decimal


def merchants_lisbon() -> list[dict]:
    """Seven merchants on four legs. Ports are used when the world runs over real HTTP."""
    return [
        dict(id="atlantico-air", name="Atlantico Air", port=8101, mcc=4511, currency="USD", ttl_min=60, kinds=["flight"],
             offers=[
                 Offer(merchant_id="atlantico-air", offer_id="SFO-LIS-1S", leg_kind="flight", title="SFO-LIS return, 1 stop",
                       unit_price=D("1180.00"), currency="USD", mcc=4511, available=9,
                       attrs={"stops": 1, "cabin": "economy", "refundable": False, "reservation_minutes": 60}),
                 Offer(merchant_id="atlantico-air", offer_id="SFO-LIS-NS", leg_kind="flight", title="SFO-LIS return, nonstop",
                       unit_price=D("1460.00"), currency="USD", mcc=4511, available=4,
                       attrs={"stops": 0, "cabin": "economy", "refundable": False, "reservation_minutes": 60})]),
        dict(id="meridian-airways", name="Meridian Airways", port=8102, mcc=4511, currency="USD", ttl_min=60,
             kinds=["flight"], offers=[
                 Offer(merchant_id="meridian-airways", offer_id="SFO-LIS-M1", leg_kind="flight",
                       title="SFO-LIS return, 1 stop", unit_price=D("1240.00"), currency="USD", mcc=4511, available=20,
                       attrs={"stops": 1, "cabin": "economy", "refundable": False, "reservation_minutes": 60})]),
        dict(id="baixa-palace", name="Hotel Baixa Palace", port=8103, mcc=7011, currency="EUR", ttl_min=30,
             kinds=["hotel"], offers=[
                 Offer(merchant_id="baixa-palace", offer_id="DLX-3N", leg_kind="hotel",
                       title="Deluxe double, 3 nights, 0.4 km from venue", unit_price=D("630.00"), currency="EUR",
                       mcc=7011, available=3,
                       attrs={"stars": 4, "distance_km": "0.4", "nights": 3, "refundable": False, "reservation_minutes": 30})]),
        dict(id="alfama-suites", name="Alfama Suites", port=8104, mcc=7011, currency="EUR", ttl_min=30, kinds=["hotel"],
             offers=[
                 Offer(merchant_id="alfama-suites", offer_id="STD-3N", leg_kind="hotel",
                       title="Superior room, 3 nights, 1.2 km from venue", unit_price=D("645.00"), currency="EUR",
                       mcc=7011, available=8,
                       attrs={"stars": 4, "distance_km": "1.2", "nights": 3, "refundable": True, "reservation_minutes": 30}),
                 Offer(merchant_id="alfama-suites", offer_id="ECO-3N", leg_kind="hotel",
                       title="Standard room, 3 nights, 0.5 km from venue", unit_price=D("450.00"), currency="EUR",
                       mcc=7011, available=10,
                       attrs={"stars": 3, "distance_km": "0.5", "nights": 3, "refundable": True, "reservation_minutes": 30})]),
        dict(id="lisbon-payments-summit", name="Lisbon Payments Summit", port=8105, mcc=7399, currency="EUR", ttl_min=45,
             kinds=["event"], offers=[
                 Offer(merchant_id="lisbon-payments-summit", offer_id="PASS-3D", leg_kind="event",
                       title="Three-day delegate pass", unit_price=D("690.00"), currency="EUR", mcc=7399, available=40,
                       attrs={"days": 3, "refundable": False, "reservation_minutes": 45})]),
        dict(id="taberna-do-largo", name="Taberna do Largo", port=8106, mcc=5812, currency="EUR", ttl_min=120,
             kinds=["restaurant"], offers=[
                 Offer(merchant_id="taberna-do-largo", offer_id="DIN-6-THU", leg_kind="restaurant",
                       title="Private table for 6, Thursday 20:00, set menu", unit_price=D("510.00"), currency="EUR",
                       mcc=5812, available=1,
                       attrs={"seats": 6, "rating": "4.7", "distance_km": "0.3", "refundable": True,
                              "reservation_minutes": 120})]),
        dict(id="casa-ribeira", name="Casa Ribeira", port=8107, mcc=5812, currency="EUR", ttl_min=120,
             kinds=["restaurant"], offers=[
                 Offer(merchant_id="casa-ribeira", offer_id="DIN-6-THU-CR", leg_kind="restaurant",
                       title="Table for 6, Thursday 20:30, tasting menu", unit_price=D("560.00"), currency="EUR",
                       mcc=5812, available=2,
                       attrs={"seats": 6, "rating": "4.6", "distance_km": "1.4", "refundable": True,
                              "reservation_minutes": 120}),
                 Offer(merchant_id="casa-ribeira", offer_id="DIN-6-THU-CHEF", leg_kind="restaurant",
                       title="Chef's counter for 6, Thursday 20:00", unit_price=D("720.00"), currency="EUR",
                       mcc=5812, available=1,
                       attrs={"seats": 6, "rating": "4.8", "distance_km": "0.6", "refundable": True,
                              "reservation_minutes": 120})]),
    ]


def mandate_lisbon(mandate_id: str = "mnd_lisbon_q4") -> Mandate:
    now = CLOCK.now().replace(microsecond=0)
    return Mandate(
        mandate_id=mandate_id,
        principal=Principal(name="Founder, Northwind Labs", organisation="Northwind Labs Inc.", email=None),
        purpose="Three people to a customer meeting and the Lisbon Payments Summit, Tue to Fri",
        home_currency="USD", cap=D("9400.00"), atomic=True, commit_by=now + timedelta(hours=2),
        legs=[
            LegSpec(leg_id="flights", kind="flight", title="3 return flights SFO-LIS", currency="USD", mcc=4511,
                    quantity=3, max_price=D("3900.00"), require=[Cond(attr="stops", op="le", value=1)],
                    substitution=Substitution(allowed=True, require=[Cond(attr="stops", op="le", value=1)],
                                              max_price=D("4200.00"))),
            LegSpec(leg_id="hotel", kind="hotel", title="3 rooms, 3 nights, near the venue", currency="EUR", mcc=7011,
                    quantity=3, max_price=D("1950.00"),
                    require=[Cond(attr="stars", op="ge", value=4), Cond(attr="distance_km", op="le", value="1.0")],
                    substitution=Substitution(allowed=True, require=[Cond(attr="stars", op="ge", value=4),
                                                                     Cond(attr="distance_km", op="le", value="1.5")],
                                              max_price=D("2100.00"))),
            LegSpec(leg_id="passes", kind="event", title="3 Lisbon Payments Summit passes", currency="EUR", mcc=7399,
                    quantity=3, max_price=D("2070.00"), preferred_merchant="lisbon-payments-summit"),
            LegSpec(leg_id="dinner", kind="restaurant", title="Client dinner for 6, Thursday", currency="EUR", mcc=5812,
                    quantity=1, max_price=D("600.00"),
                    require=[Cond(attr="distance_km", op="le", value="1.0"), Cond(attr="rating", op="ge", value="4.5")],
                    substitution=Substitution(allowed=True,
                                              require=[Cond(attr="distance_km", op="le", value="1.0"),
                                                       Cond(attr="rating", op="ge", value="4.5")],
                                              max_price=D("650.00"))),
        ])


def merchants_hardware() -> list[dict]:
    """A 500-unit pilot run of a handheld device: chips, boards and screens from three suppliers."""
    def o(mid, oid, title, price, ccy, avail, **attrs):
        return Offer(merchant_id=mid, offer_id=oid, leg_kind="component", title=title, unit_price=D(price),
                     currency=ccy, mcc=5065, available=avail, attrs={"reservation_minutes": 240, **attrs})
    return [
        dict(id="corvid-semi", name="Corvid Semiconductors", port=8111, mcc=5065, currency="USD", ttl_min=240,
             kinds=["component"], offers=[o("corvid-semi", "MCU-R7", "R7 microcontroller, reel", "3.10", "USD", 600,
                                            part="mcu", grade="industrial", refundable=False)]),
        dict(id="northbridge", name="Northbridge Components", port=8112, mcc=5065, currency="USD", ttl_min=240,
             kinds=["component"], offers=[o("northbridge", "MCU-R7-NB", "R7 microcontroller, cut tape", "3.35", "USD",
                                            5000, part="mcu", grade="industrial", refundable=True)]),
        dict(id="lianfa-pcb", name="Lianfa PCB", port=8113, mcc=5065, currency="CNY", ttl_min=240, kinds=["component"],
             offers=[o("lianfa-pcb", "PCB-4L", "4-layer main board, assembled", "18.50", "CNY", 700, part="pcb",
                       layers=4, refundable=False)]),
        dict(id="huaxin-pcb", name="Huaxin Electronics", port=8114, mcc=5065, currency="CNY", ttl_min=240,
             kinds=["component"], offers=[o("huaxin-pcb", "PCB-4L-HX", "4-layer main board, assembled", "19.80", "CNY",
                                            3000, part="pcb", layers=4, refundable=False)]),
        dict(id="lumatek", name="Lumatek Displays", port=8115, mcc=5065, currency="EUR", ttl_min=240,
             kinds=["component"], offers=[o("lumatek", "LCD-24", "2.4 in IPS display", "6.40", "EUR", 550, part="lcd",
                                            refundable=False)]),
        dict(id="rhein-optics", name="Rhein Optics", port=8116, mcc=5065, currency="EUR", ttl_min=240,
             kinds=["component"], offers=[o("rhein-optics", "LCD-24-RO", "2.4 in IPS display", "7.10", "EUR", 300,
                                            part="lcd", refundable=True)]),
    ]


def mandate_hardware(mandate_id: str = "mnd_pilot_500") -> Mandate:
    now = CLOCK.now().replace(microsecond=0)
    def leg(leg_id, title, ccy, max_price, part, sub_max):
        return LegSpec(leg_id=leg_id, kind="component", title=title, currency=ccy, mcc=5065, quantity=500,
                       max_price=D(max_price), require=[Cond(attr="part", op="eq", value=part)],
                       substitution=Substitution(allowed=True, require=[Cond(attr="part", op="eq", value=part)],
                                                 max_price=D(sub_max)))
    return Mandate(mandate_id=mandate_id, principal=Principal(name="COO, Halyard Devices", organisation="Halyard Devices"),
                   purpose="Parts for a 500-unit pilot run; useless unless all three arrive", home_currency="USD",
                   cap=D("7000.00"), atomic=True, commit_by=now + timedelta(hours=6),
                   legs=[leg("mcu", "500 R7 microcontrollers", "USD", "1600.00", "mcu", "1700.00"),
                         leg("pcb", "500 assembled main boards", "CNY", "9500.00", "pcb", "9950.00"),
                         leg("lcd", "500 2.4 in displays", "EUR", "3300.00", "lcd", "3600.00")])
