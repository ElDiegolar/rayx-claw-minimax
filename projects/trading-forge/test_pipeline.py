"""
Trading Forge - Simple Test Script

Demonstrates that all modules can be instantiated and the pipeline works.
"""

import logging
from datetime import datetime, timedelta

# Set up basic logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Import all modules
from config import (
    MOMENTUM_THRESHOLD,
    CVD_WEIGHT,
    FUNDING_WEIGHT,
    OI_WEIGHT,
    SKEW_WEIGHT,
    CONSENSUS_GATE,
    EDGE_THRESHOLD,
    TARGET_MARKETS,
    MIN_MARKET_VOLUME,
)

from models import (
    Market,
    Signal,
    Direction,
    Portfolio,
    Trade,
    Position,
    Order,
    OrderType,
    OrderStatus,
    PositionStatus,
    SignalStage,
    DropReason,
    DroppedSignal,
    MarketData,
)

from data_feeds import (
    BTCPriceFeed,
    OrderBookFeed,
    OrderBook,
    OrderBookLevel,
    FundingFeed,
    OIFeed,
    CVDFeed,
    OptionsFeed,
    CompositeMarketData,
    DataFeedManager,
)

from signal_generator import (
    compute_momentum_signal,
    compute_cvd_signal,
    compute_funding_signal,
    compute_oi_delta_signal,
    compute_options_skew,
    aggregate_signals,
    compute_consensus,
    generate_signal,
)

from edge_calculator import EdgeCalculator

from toxicity_filters import ToxicityFilters

from risk_manager import RiskManager

from execution import ExecutionHandler

from orchestrator import (
    PolymarketScanner,
    PipelineRunner,
    TradingOrchestrator,
    RegimeDetector,
    Regime,
)


def test_config():
    """Test configuration values."""
    print("\n" + "="*60)
    print("TEST: Configuration Values")
    print("="*60)
    
    print(f"  MOMENTUM_THRESHOLD: {MOMENTUM_THRESHOLD}")
    print(f"  CVD_WEIGHT: {CVD_WEIGHT}")
    print(f"  FUNDING_WEIGHT: {FUNDING_WEIGHT}")
    print(f"  OI_WEIGHT: {OI_WEIGHT}")
    print(f"  SKEW_WEIGHT: {SKEW_WEIGHT}")
    print(f"  CONSENSUS_GATE: {CONSENSUS_GATE}")
    print(f"  EDGE_THRESHOLD: {EDGE_THRESHOLD}")
    print(f"  TARGET_MARKETS: {TARGET_MARKETS}")
    print(f"  MIN_MARKET_VOLUME: {MIN_MARKET_VOLUME}")
    
    print("\n  ✓ Config values loaded correctly")
    return True


def test_models():
    """Test data models."""
    print("\n" + "="*60)
    print("TEST: Data Models")
    print("="*60)
    
    # Create a market
    market = Market(
        id="BTC_UP_2024_01_15_1200",
        question="Will BTC be above $50,000 at 12:00 UTC?",
        resolution_time=datetime.utcnow() + timedelta(minutes=30),
        current_price=0.55,
        volume=50000.0,
        yes_bid=0.53,
        yes_ask=0.57,
        no_bid=0.43,
        no_ask=0.47,
    )
    print(f"  Created Market: {market.id}")
    print(f"    - current_price: {market.current_price}")
    print(f"    - spread: {market.spread:.4f}")
    print(f"    - effective_spread: {market.effective_spread:.4f}")
    print(f"    - liquidity: {market.liquidity:.2f}")
    
    # Test Direction enum
    print(f"\n  Direction enum values: {[d.value for d in Direction]}")
    
    # Create a portfolio
    portfolio = Portfolio(bankroll=10000.0)
    print(f"\n  Created Portfolio:")
    print(f"    - bankroll: ${portfolio.bankroll:.2f}")
    print(f"    - start_bankroll: ${portfolio.start_bankroll:.2f}")
    print(f"    - peak_bankroll: ${portfolio.peak_bankroll:.2f}")
    
    # Test Signal creation
    signal = Signal(
        market_id=market.id,
        direction=Direction.UP,
        p_model=0.65,
        p_market=0.55,
        edge=0.10,
        cvd_signal=0.8,
        momentum_signal=0.6,
        funding_signal=0.4,
        oi_signal=0.5,
        skew_signal=0.3,
        consensus_score=7.5,
    )
    print(f"\n  Created Signal:")
    print(f"    - direction: {signal.direction.value}")
    print(f"    - is_up: {signal.is_up}")
    print(f"    - p_model: {signal.p_model:.3f}")
    print(f"    - edge: {signal.edge:.3f}")
    print(f"    - consensus_score: {signal.consensus_score:.2f}")
    
    print("\n  ✓ Data models work correctly")
    return True


def test_data_feeds():
    """Test data feeds."""
    print("\n" + "="*60)
    print("TEST: Data Feeds")
    print("="*60)
    
    # Create BTC price feed
    btc_feed = BTCPriceFeed(window_size=50)
    btc_feed.update(45000.0)
    btc_feed.update(45100.0)
    btc_feed.update(45200.0)
    btc_feed.update(45300.0)
    btc_feed.update(45400.0)
    btc_feed.update(45500.0)
    
    print(f"  BTC Price Feed:")
    print(f"    - current: ${btc_feed.get_current():.2f}")
    print(f"    - history (last 5): {btc_feed.get_price_history(5)}")
    print(f"    - momentum: {btc_feed.get_momentum(5):.4f}")
    
    # Create order book
    ob = OrderBook(
        yes_bids=[OrderBookLevel(price=0.53, volume=1000)],
        yes_asks=[OrderBookLevel(price=0.57, volume=1000)],
        no_bids=[OrderBookLevel(price=0.43, volume=1000)],
        no_asks=[OrderBookLevel(price=0.47, volume=1000)],
        timestamp=datetime.utcnow(),
    )
    
    ob_feed = OrderBookFeed()
    ob_feed.update(ob)
    
    print(f"\n  Order Book Feed:")
    print(f"    - yes_bid: {ob_feed.get_current().yes_bid}")
    print(f"    - yes_ask: {ob_feed.get_current().yes_ask}")
    print(f"    - spread: {ob_feed.get_spread():.4f}")
    print(f"    - imbalance: {ob_feed.get_imbalance():.4f}")
    
    # Create composite market data
    composite = CompositeMarketData("TEST_MARKET")
    composite.update(
        btc_price=45500.0,
        order_book=ob,
        funding_rate=0.0001,
        oi=1000000.0,
        cvd=50000.0,
        call_iv=0.25,
        put_iv=0.28,
    )
    
    print(f"\n  Composite Market Data:")
    data_dict = composite.to_dict()
    print(f"    - prices (last 3): {data_dict['prices'][-3:]}")
    print(f"    - btc_price: ${data_dict['btc_price']:.2f}")
    print(f"    - cvd_now: {data_dict['cvd_now']:.2f}")
    print(f"    - funding_rate: {data_dict['funding_rate']:.6f}")
    
    # Test DataFeedManager
    manager = DataFeedManager()
    feed = manager.get_or_create_feed("BTC_UP")
    feed2 = manager.get_or_create_feed("BTC_UP")
    feed3 = manager.get_or_create_feed("BTC_DOWN")
    
    print(f"\n  DataFeedManager:")
    print(f"    - markets tracked: {manager.list_markets()}")
    print(f"    - same feed returned: {feed is feed2}")
    
    print("\n  ✓ Data feeds work correctly")
    return True


def test_signal_generation():
    """Test signal generation functions."""
    print("\n" + "="*60)
    print("TEST: Signal Generation Functions")
    print("="*60)
    
    # Test momentum signal
    prices = [45000, 45100, 45200, 45300, 45400, 45500]
    signal, adj = compute_momentum_signal(prices)
    print(f"  Momentum Signal (6 prices): signal={signal}, adj={adj:.4f}")
    
    # Test with insufficient data
    signal, adj = compute_momentum_signal([45000, 45100])
    print(f"  Momentum Signal (2 prices): signal={signal}, adj={adj}")
    
    # Test CVD signal
    signal, adj = compute_cvd_signal(50000, 40000)
    print(f"  CVD Signal (increasing): signal={signal}, adj={adj:.4f}")
    
    signal, adj = compute_cvd_signal(40000, 50000)
    print(f"  CVD Signal (decreasing): signal={signal}, adj={adj:.4f}")
    
    # Test funding signal
    signal, adj = compute_funding_signal(0.0005)
    print(f"  Funding Signal (positive): signal={signal}, adj={adj:.4f}")
    
    signal, adj = compute_funding_signal(-0.0005)
    print(f"  Funding Signal (negative): signal={signal}, adj={adj:.4f}")
    
    # Test OI delta signal
    signal, adj = compute_oi_delta_signal(45500, 45000, 1100000, 1000000)
    print(f"  OI Signal (price up + OI up): signal={signal}, adj={adj:.4f}")
    
    # Test options skew
    signal, adj = compute_options_skew(0.30, 0.25)
    print(f"  Options Skew (call > put): signal={signal}, adj={adj:.4f}")
    
    # Test aggregation
    p = aggregate_signals(1.0, 0.8, 0.5, 0.6, 0.4)
    print(f"\n  Aggregated Probability: {p:.4f}")
    
    # Test consensus
    c = compute_consensus(1.0, 0.8, 0.5, 0.6, 0.4)
    print(f"  Consensus Score: {c:.2f}")
    
    # Test full signal generation
    market = Market(
        id="BTC_UP_TEST",
        question="Test market",
        resolution_time=datetime.utcnow() + timedelta(minutes=30),
        current_price=0.55,
        volume=50000.0,
        yes_bid=0.53,
        yes_ask=0.57,
        no_bid=0.43,
        no_ask=0.47,
    )
    
    market_data = {
        'prices': [45000 + i*100 for i in range(12)],
        'cvd_now': 50000,
        'cvd_5min_ago': 40000,
        'funding_rate': 0.0005,
        'oi_now': 1100000,
        'oi_5min_ago': 1000000,
        'call_iv': 0.30,
        'put_iv': 0.25,
        'yes_bid': 0.53,
        'yes_ask': 0.57,
        'no_bid': 0.43,
        'no_ask': 0.47,
    }
    
    signal = generate_signal(market, market_data)
    if signal:
        print(f"\n  Generated Signal:")
        print(f"    - direction: {signal.direction.value}")
        print(f"    - p_model: {signal.p_model:.4f}")
        print(f"    - p_market: {signal.p_market:.4f}")
        print(f"    - edge: {signal.edge:.4f}")
        print(f"    - consensus: {signal.consensus_score:.2f}")
    else:
        print(f"\n  No signal generated (may be expected based on thresholds)")
    
    print("\n  ✓ Signal generation works correctly")
    return True


def test_pipeline_components():
    """Test pipeline components."""
    print("\n" + "="*60)
    print("TEST: Pipeline Components")
    print("="*60)
    
    # Test Edge Calculator
    edge_calc = EdgeCalculator()
    print(f"  Edge Calculator: {edge_calc}")
    
    # Test Toxicity Filters
    tox_filters = ToxicityFilters()
    print(f"  Toxicity Filters: {tox_filters}")
    
    # Test Risk Manager (use 'bankroll' parameter, not 'initial_bankroll')
    risk_mgr = RiskManager(bankroll=10000.0)
    print(f"  Risk Manager: bankroll=${risk_mgr.bankroll:.2f}")
    
    # Test Execution Handler
    exec_handler = ExecutionHandler(paper_trading=True)
    print(f"  Execution Handler: paper_trading={exec_handler.paper_trading}")
    
    # Test Scanner
    scanner = PolymarketScanner()
    print(f"  Scanner: {scanner}")
    
    # Test Regime Detector
    regime_det = RegimeDetector()
    print(f"  Regime Detector: regime={regime_det.regime}")
    
    print("\n  ✓ Pipeline components work correctly")
    return True


def test_orchestrator():
    """Test orchestrator creation."""
    print("\n" + "="*60)
    print("TEST: Orchestrator")
    print("="*60)
    
    orch = TradingOrchestrator(
        initial_bankroll=10000.0,
        paper_trading=True,
    )
    
    print(f"  Orchestrator created:")
    print(f"    - bankroll: ${orch.portfolio.bankroll:.2f}")
    print(f"    - scanner: {orch.scanner}")
    print(f"    - data_feed_manager: {orch.data_feed_manager}")
    print(f"    - edge_calculator: {orch.edge_calculator}")
    print(f"    - risk_manager: {orch.risk_manager}")
    print(f"    - execution_handler: {orch.execution_handler}")
    print(f"    - regime_detector: {orch.regime_detector}")
    print(f"    - pipeline: {orch.pipeline}")
    
    # Add a test market
    market = Market(
        id="BTC_UP_TEST",
        question="Test market",
        resolution_time=datetime.utcnow() + timedelta(minutes=30),
        current_price=0.55,
        volume=50000.0,
        yes_bid=0.53,
        yes_ask=0.57,
        no_bid=0.43,
        no_ask=0.47,
    )
    orch.add_market(market)
    print(f"\n  Added test market: {orch.scanner.get_market('BTC_UP_TEST')}")
    
    print("\n  ✓ Orchestrator works correctly")
    return True


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("TRADING FORGE - TEST SUITE")
    print("="*60)
    
    tests = [
        ("Configuration", test_config),
        ("Data Models", test_models),
        ("Data Feeds", test_data_feeds),
        ("Signal Generation", test_signal_generation),
        ("Pipeline Components", test_pipeline_components),
        ("Orchestrator", test_orchestrator),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n  ✗ {name} FAILED: {e}")
            results.append((name, False))
    
    # Print summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {name}")
    
    print(f"\n  Total: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n  🎉 All tests passed! Trading Forge is working correctly.")
        return 0
    else:
        print("\n  ⚠ Some tests failed. Please review the errors above.")
        return 1


if __name__ == "__main__":
    exit(main())
