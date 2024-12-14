import json
import argparse
import shutil

from random import getrandbits
from hashlib import sha1
from base64 import b32encode
from pathlib import Path
from dataclasses import dataclass, field
from typing import TypeVar, Type, Any


def mk_uid(data: str) -> str:
    return b32encode(sha1(str(data).encode()).digest()[4:9]).decode()

def mk_player_uid(data: str) -> str:
    return data.split("-")[-1]


@dataclass
class Index:
    uid: str
    title: str
    date: int
    payout: int
    total: int

    def warp(self) -> dict[str, Any]:
        return {
            "uuid": self.uid,
            "title": self.title,
            "date": self.date,
            "payout": self.payout,
            "total": self.total
        }

class IndexReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.indexes: list[Index] = []

    def add_index(self, index: Index) -> None:
        self.indexes.append(index)

    def dump(self) -> None:
        with self.path.open("w") as fp:
            json.dump({
                "records": [x.warp() for x in self.indexes]
            }, fp)

    def load(self) -> None:
        with self.path.open("r") as fp:
            data = json.load(fp)
            for record in data["records"]:
                record['uid'] = record['uuid']
                del record['uuid']
                self.add_index(Index(**record))

@dataclass
class Player:
    uid: str
    name: str
    # guild: str
    class_: str
    race: str

    def warp(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "class": self.class_,
            "race": self.race,
        }


@dataclass
class Auction:
    uid: str
    price: int
    itemID: int

    winner_uid: str
    note: str = ""

    def __post_init__(self) -> None:
        self.uid = mk_uid(self.uid)

    def guess_item_type(self) -> int:
        if self.itemID == 45978:
            if self.note == "Fine":
                return 8
            else:
                return 9

        return 1

    def warp(self) -> dict[str, Any]:
        return {
            "uuid": self.uid,
            "amount": self.price,
            "itemID": self.itemID,
            "playerId": self.winner_uid,
            "note": self.note,
            "itemType": self.guess_item_type()
        }

@dataclass
class GoldLedger:
    cut: int = 0    # cut
    paid: int = 0   # pay for auctions and fines
    received: int = 0   # gold received from trade
    # payout below.
    given: int = 0      # gold given to players (trade)
    mailed: int = 0     # gold mailed to players

    def warp(self, player_uid: str) -> dict[str, Any] | None:
        if self.cut == 0:
            return None

        return {
            "playerID": player_uid,
            "cut": self.cut,
            "paid": self.paid,
            "received": self.received,
            "given": self.given,
            "mailed": self.mailed,
        }

@dataclass
class GDKPInstance:
    uid: str
    title: str
    date: int
    players: dict[str, Player]
    auctions: list[Auction]
    ledgers: dict[str, GoldLedger] = field(default_factory=dict)
    misc: dict[str, Any] = field(default_factory=dict)

    def set_uid(self, uid: str) -> None:
        self.uid = mk_uid(uid)

    def warp(self) -> dict[str, Any]:
        return {
            "uuid": self.uid,
            "title": self.title,
            "date": self.date,
            "players": dict([(k, v.warp()) for k, v in self.players.items()]),
            "items": [x.warp() for x in self.auctions],
            "ledgers": [
                x for x in
                [v.warp(k) for k, v in self.ledgers.items()]
                if x is not None
            ],
            "misc": self.misc,
        }

G = TypeVar('G', bound='GDKPReader')

class GDKPReader:

    def __init__(self, path: Path) -> None:
        self.path = path
        self.instance = GDKPInstance("", "", 0, {}, [])

    @classmethod
    def read_file(cls: Type[G], path: Path) -> G:
        instance = cls(path)
        instance.read()

        return instance

    def to_index(self) -> Index:
        return Index(
            self.instance.uid,
            self.instance.title,
            self.instance.date,
            self.instance.misc['payout'],
            self.instance.misc['incoming']
        )

    def read(self) -> None:
        with self.path.open() as fp:
            ctx = json.load(fp)

        # set title & misc
        self.instance.title = ctx["title"]
        self.instance.date = ctx["createdAt"]
        self.instance.set_uid(ctx["ID"])

        self.instance.misc['payout'] = int(ctx['lastAvailableBase'])
        self.instance.misc['incoming'] = 0

        # parse auctions
        for auction_data in ctx['Auctions'].values():
            self.parse_auction(auction_data)

        # parse trade log
        for player, data in ctx['GoldLedger'].items():
            self.parse_trade_log(player, data)

        # parse cut
        self.parse_cut(ctx['Pot']['Cuts'])

    def parse_auction(self, data) -> None:
        price = data.get('price', 0)
        if price == 0:
            return

        player_uid = mk_player_uid(data['Winner']['uuid'])
        auction = Auction(
            data['ID'],
            price,
            data['itemID'],
            player_uid,
            data.get("note", "")
        )
        self.instance.auctions.append(auction)

        self.add_player(data['Winner'])
        self.instance.ledgers[player_uid].paid += price
        self.instance.misc['incoming'] += price

        # parse player infos
        for bid in data.get('Bids', {}).values():
            self.add_player(bid['Bidder'])

    def add_player(self, data):
        uid = mk_player_uid(data['uuid'])

        if uid in self.instance.players:
            return

        player = Player(
            uid,
            data['name'],
            # data['guild'],
            data['class'].lower(),
            data['race'].lower()
        )
        self.instance.players[uid] = player
        self.instance.ledgers[uid] = GoldLedger()

    def parse_trade_log(self, player, data):
        # find player uid
        player_obj: GoldLedger | None = None

        player_name = player.split("-")[0]
        for uid, player_info in self.instance.players.items():
            if player_info.name.lower() == player_name.lower():
                player_obj = self.instance.ledgers[uid]
                break

        if player_obj is None:
            # print("[!] Player not found:", player)
            # if player not found, create a dump player.
            uid = "FF" + getrandbits(24).to_bytes(3).hex().upper()
            self.instance.players[uid] = Player(uid, player_name.capitalize(), "", "")
            player_obj = GoldLedger()
            self.instance.ledgers[uid] = player_obj

        for log_data in data.values():
            if log_data['type'] == "trade":
                player_obj.received += int(log_data['received'] / 10000)
                player_obj.given += int(log_data['given'] / 10000)
            elif log_data['type'] == "mail":
                player_obj.mailed += int(log_data['given'] / 10000)

    def parse_cut(self, data):
        for player, cut in data.items():
            player_name = player.split("-")[0]
            for uid, player_info in self.instance.players.items():
                if player_info.name.lower() == player_name.lower():
                    self.instance.ledgers[uid].cut = int(cut)
                    break

def do_rebuild(root_path: Path, dest_path: Path):
    print("[*] Rebuilding...")
    if not root_path.exists() or not dest_path.exists():
        print("  [!] Path not found:", root_path, dest_path)
        return

    json_dest = dest_path / "records"
    json_dest.mkdir(exist_ok=True)

    # create index
    gindex = IndexReader(dest_path / "index.json")

    for record in root_path.glob("*.json"):
        print("  + Processing:", record.name)
        g = GDKPReader.read_file(record)
        gindex.add_index(g.to_index())
        with open(json_dest / f"{g.instance.uid}.json", "w") as f:
            json.dump(g.instance.warp(), f)

    gindex.dump()
    print("  [!] Done.")


def do_add( add_path: Path, root_path: Path, dest_path: Path):
    print("[*] Add Json from Pull requests...")
    if not add_path.exists() or not root_path.exists() or not dest_path.exists():
        print("  [!] Some Path not found.")
        return

    json_dest = dest_path / "records"

    gindex = IndexReader(dest_path / "index.json")
    gindex.load()

    # try to find json file in root.
    for record in add_path.glob("*.json"):
        print("  + Processing:", record.name)
        g = GDKPReader.read_file(record)
        gindex.add_index(g.to_index())
        with open(json_dest / f"{g.instance.uid}.json", "w") as f:
            json.dump(g.instance.warp(), f)

        # copy file to root_path
        shutil.copy(record, root_path / record.name)

    gindex.dump()
    print("  [!] Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--addjson', action='store_true')
    parser.add_argument('-a', '--add-path', type=Path)
    parser.add_argument('-r', '--root-path', type=Path)
    parser.add_argument('-d', '--dest-path', type=Path)

    args = parser.parse_args()

    if args.rebuild:
        do_rebuild(args.root_path, args.dest_path)
    elif args.addjson:
        do_add(args.add_path, args.root_path, args.dest_path)

if __name__ == "__main__":
    main()
