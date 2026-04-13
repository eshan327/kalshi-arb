from feeds.exchanges.base import ExchangeAdapter
from feeds.exchanges.bitstamp import BitstampAdapter, stream as stream_bitstamp
from feeds.exchanges.coinbase import CoinbaseAdapter, stream as stream_coinbase
from feeds.exchanges.gemini import GeminiAdapter, stream as stream_gemini
from feeds.exchanges.kraken import KrakenAdapter, stream as stream_kraken
from feeds.exchanges.paxos import PaxosAdapter, stream as stream_paxos

__all__ = [
    "ExchangeAdapter",
    "CoinbaseAdapter",
    "KrakenAdapter",
    "GeminiAdapter",
    "BitstampAdapter",
    "PaxosAdapter",
    "stream_coinbase",
    "stream_kraken",
    "stream_gemini",
    "stream_bitstamp",
    "stream_paxos",
]
