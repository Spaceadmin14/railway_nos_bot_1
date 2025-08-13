#!/usr/bin/env python3
"""
Bot de market making MEXC avec API REST pour les données publiques
"""

import asyncio
import os
import math
import signal
import sys
from typing import Dict, Optional

import ccxt.pro as ccxt  # type: ignore

# -------------------------------------------------------------
# Faster event-loop (optional). uvloop works on Linux/macOS.
try:
    import uvloop  # type: ignore
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

# ──────────────────────────────────────────────────────────────
# Configuration – tweak these as needed
# ──────────────────────────────────────────────────────────────
SYMBOL = os.getenv("MEXC_SYMBOL", "NOS/USDT")

SPREAD_THRESHOLD_PCT = float(os.getenv("SPREAD_THRESHOLD_PCT", "1")) / 100  # 1%
CHECK_INTERVAL_SEC = 0.5  # 0.5 seconde entre les vérifications (plus rapide)
ORDER_VALUE_USDT_BUY = float(os.getenv("ORDER_VALUE_USDT_BUY", "1.50"))  # buy side order USDT value
ORDER_VALUE_USDT_SELL = float(os.getenv("ORDER_VALUE_USDT_SELL", "1.50"))  # sell side order USDT value

# Read credentials from env-vars for safety (never hard-code keys)
API_KEY = "mx0vglRzl0hnDcxPds"
API_SECRET = "d5808e518f084f4f903a04478fcb4c50"

# Global flag for graceful shutdown
shutdown_requested = False

# ──────────────────────────────────────────────────────────────
# Helper utilities
# ──────────────────────────────────────────────────────────────

def _round_to(amount: float, precision) -> float:
    """Return *amount* rounded to exchange precision."""
    if isinstance(precision, int):
        fmt = f"{{:.{precision}f}}"
        return float(fmt.format(amount))

    step = float(precision)
    if step <= 0:
        return amount
    decimals = max(0, int(round(-math.log10(step))))
    fmt = f"{{:.{decimals}f}}"
    rounded = round(amount / step) * step
    return float(fmt.format(rounded))

def _tick_size(market: Dict) -> float:
    """Infer tick size (price increment)."""
    prec = market.get("precision", {}).get("price", None)
    if prec is None:
        return 0.0
    if isinstance(prec, int):
        return 10 ** (-prec)
    return prec

async def _cancel_and_create(
    mexc,
    symbol: str,
    side: str,
    old_id: Optional[str],
    qty: float,
    price: float,
):
    """Atomically cancel *old_id* (if given) and submit a new post-only order."""
    try:
        if old_id is not None:
            await mexc.cancel_order(old_id, symbol)
    except Exception as e:
        print(f"[WARN] Failed to cancel order {old_id}: {e}")

    try:
        order = await mexc.create_order(
            symbol=symbol,
            type="LIMIT_MAKER",
            side=side,
            amount=qty,
            price=price,
            params={"postOnly": True},
        )
        return order["id"], price
    except Exception as e:
        print(f"[ERROR] Failed to place {side} order: {e}")
        return None, None

async def _order_watcher(mexc, symbol: str, state: Dict[str, Optional[float]]) -> None:
    """Keeps *state* up-to-date with order status using REST API."""
    global shutdown_requested
    
    while not shutdown_requested:
        try:
            # Fetch open orders via REST API instead of WebSocket
            open_orders = await mexc.fetch_open_orders(symbol)
            
            # STRICT VERIFICATION: Ensure only 2 orders maximum
            our_orders = []
            for order in open_orders:
                if order["side"] == "buy" and order["id"] == state.get("bid_order_id"):
                    our_orders.append(("buy", order["id"]))
                elif order["side"] == "sell" and order["id"] == state.get("ask_order_id"):
                    our_orders.append(("sell", order["id"]))
            
            # CRITICAL: If more than 2 orders found, cancel extras
            if len(our_orders) > 2:
                print(f"[CRITICAL] Found {len(our_orders)} orders! Cancelling extras...")
                print(f"[CRITICAL] Our orders: {our_orders}")
                
                # Cancel all orders and reset state
                for side, order_id in our_orders:
                    try:
                        await mexc.cancel_order(order_id, symbol)
                        print(f"[CRITICAL] Cancelled extra {side} order: {order_id}")
                    except Exception as e:
                        print(f"[CRITICAL] Failed to cancel {side} order {order_id}: {e}")
                
                # Reset state completely
                state["bid_order_id"] = None
                state["ask_order_id"] = None
                state["bid_price_live"] = None
                state["ask_price_live"] = None
                print("[CRITICAL] State reset - will recreate orders")
                continue
            
            # Update state based on open orders
            bid_found = False
            ask_found = False
            
            for order in open_orders:
                if order["side"] == "buy" and order["id"] == state.get("bid_order_id"):
                    bid_found = True
                elif order["side"] == "sell" and order["id"] == state.get("ask_order_id"):
                    ask_found = True
            
            # Clear state if orders are no longer open
            if not bid_found and state.get("bid_order_id"):
                print(f"[INFO] Bid order {state['bid_order_id']} no longer open")
                state["bid_order_id"] = None
                state["bid_price_live"] = None
                
            if not ask_found and state.get("ask_order_id"):
                print(f"[INFO] Ask order {state['ask_order_id']} no longer open")
                state["ask_order_id"] = None
                state["ask_price_live"] = None
                
            # Log current order count
            current_count = len([o for o in [state.get("bid_order_id"), state.get("ask_order_id")] if o is not None])
            print(f"[STATUS] Current active orders: {current_count}/2")
                
        except Exception as e:
            if shutdown_requested:
                break
            print(f"[WARN] Order watcher error: {e}")
            await asyncio.sleep(2)
        else:
            await asyncio.sleep(2)  # Check every 2 seconds

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global shutdown_requested
    print(f"\n[SHUTDOWN] Received signal {signum}, shutting down gracefully...")
    shutdown_requested = True

async def tighten_spread_rest(symbol: str = SYMBOL) -> None:
    """Tighten bid/ask spread using REST API for order book data."""
    
    global shutdown_requested

    while not shutdown_requested:
        mexc = None
        try:
            mexc = ccxt.mexc({
                "apiKey": API_KEY,
                "secret": API_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
                "timeout": 10000,  # 10 secondes au lieu de 30
                "rateLimit": 100,   # 100ms au lieu de 1000ms
            })

            await mexc.load_markets()
            if symbol not in mexc.markets:
                raise ValueError(f"Symbol {symbol!r} not available on MEXC")

            market = mexc.market(symbol)
            amount_prec = market["precision"].get("amount", 8)
            price_prec = market["precision"].get("price", 8)
            min_amount = market["limits"].get("amount", {}).get("min", 0.0) or 0.0
            tick = _tick_size(market) or 10 ** (-price_prec)

            # CLEANUP: Cancel any existing orders at startup
            print("[STARTUP] Cleaning up any existing orders...")
            try:
                existing_orders = await mexc.fetch_open_orders(symbol)
                cancelled_count = 0
                for order in existing_orders:
                    try:
                        await mexc.cancel_order(order["id"], symbol)
                        print(f"[STARTUP] Cancelled existing order: {order['id']} ({order['side']})")
                        cancelled_count += 1
                    except Exception as e:
                        print(f"[STARTUP] Failed to cancel order {order['id']}: {e}")
                if cancelled_count > 0:
                    print(f"[STARTUP] Cancelled {cancelled_count} existing orders")
                    await asyncio.sleep(1)  # Wait for cancellations to process
            except Exception as e:
                print(f"[STARTUP] Error during cleanup: {e}")

            state: Dict[str, Optional[float]] = {
                "bid_order_id": None,
                "ask_order_id": None,
                "bid_price_live": None,
                "ask_price_live": None,
            }

            updating_bid = False
            updating_ask = False

            print(
                f"[INIT] (REST) Starting tightener for {symbol} – threshold={SPREAD_THRESHOLD_PCT*100:.2f} %, "
                f"order USDT value buy={ORDER_VALUE_USDT_BUY}, sell={ORDER_VALUE_USDT_SELL} (min-qty={min_amount})"
            )
            print(f"[INIT] Fast mode: {CHECK_INTERVAL_SEC}s intervals, {mexc.rateLimit}ms rate limit")

            async def _book_loop():
                nonlocal updating_bid, updating_ask
                
                while not shutdown_requested:
                    try:
                        # Use REST API to fetch order book
                        ob = await mexc.fetch_order_book(symbol, limit=5)
                        
                        if not ob["bids"] or not ob["asks"]:
                            print("[WARN] No order book data available")
                            await asyncio.sleep(CHECK_INTERVAL_SEC)
                            continue

                        best_bid = ob["bids"][0][0]
                        best_ask = ob["asks"][0][0]
                        
                        # Analyser le 2ème ordre
                        second_bid = ob["bids"][1][0] if len(ob["bids"]) > 1 else best_bid
                        second_ask = ob["asks"][1][0] if len(ob["asks"]) > 1 else best_ask
                        

                        print(f"[INFO] Best bid: {best_bid}, Best ask: {best_ask}")
                        print(f"[INFO] 2nd bid: {second_bid}, 2nd ask: {second_ask}")

                        # Vérifier si nos ordres sont vraiment les best bid/ask
                        our_bid_is_best = False
                        our_ask_is_best = False
                        
                        # Vérification réelle : comparer nos ordres avec l'order book
                        if state["bid_price_live"] is not None and state["bid_order_id"] is not None:
                            # Notre ordre bid est-il le meilleur ?
                            if abs(state["bid_price_live"] - best_bid) <= tick:
                                # Vérification supplémentaire : notre ordre existe-t-il encore ?
                                try:
                                    open_orders = await mexc.fetch_open_orders(symbol)
                                    our_bid_exists = any(o["id"] == state["bid_order_id"] for o in open_orders)
                                    if our_bid_exists:
                                        our_bid_is_best = True
                                        print(f"[STATUS] Our bid ({state['bid_price_live']}) is the best bid")
                                    else:
                                        print(f"[WARN] Our bid order {state['bid_order_id']} no longer exists")
                                        state["bid_order_id"] = None
                                        state["bid_price_live"] = None
                                except Exception as e:
                                    print(f"[WARN] Error verifying bid order: {e}")
                            else:
                                # Notre ordre bid n'est plus le meilleur - le supprimer
                                print(f"[CRITICAL] Our bid ({state['bid_price_live']}) is no longer the best bid ({best_bid}) - cancelling")
                                try:
                                    await mexc.cancel_order(state["bid_order_id"], symbol)
                                    print(f"[CRITICAL] Cancelled outdated bid order {state['bid_order_id']}")
                                    state["bid_order_id"] = None
                                    state["bid_price_live"] = None
                                except Exception as e:
                                    print(f"[ERROR] Failed to cancel outdated bid order: {e}")
                                
                        if state["ask_price_live"] is not None and state["ask_order_id"] is not None:
                            # Notre ordre ask est-il le meilleur ?
                            if abs(state["ask_price_live"] - best_ask) <= tick:
                                # Vérification supplémentaire : notre ordre existe-t-il encore ?
                                try:
                                    open_orders = await mexc.fetch_open_orders(symbol)
                                    our_ask_exists = any(o["id"] == state["ask_order_id"] for o in open_orders)
                                    if our_ask_exists:
                                        our_ask_is_best = True
                                        print(f"[STATUS] Our ask ({state['ask_price_live']}) is the best ask")
                                    else:
                                        print(f"[WARN] Our ask order {state['ask_order_id']} no longer exists")
                                        state["ask_order_id"] = None
                                        state["ask_price_live"] = None
                                except Exception as e:
                                    print(f"[WARN] Error verifying ask order: {e}")
                            else:
                                # Notre ordre ask n'est plus le meilleur - le supprimer
                                print(f"[CRITICAL] Our ask ({state['ask_price_live']}) is no longer the best ask ({best_ask}) - cancelling")
                                try:
                                    await mexc.cancel_order(state["ask_order_id"], symbol)
                                    print(f"[CRITICAL] Cancelled outdated ask order {state['ask_order_id']}")
                                    state["ask_order_id"] = None
                                    state["ask_price_live"] = None
                                except Exception as e:
                                    print(f"[ERROR] Failed to cancel outdated ask order: {e}")

                        # Vérifier si le best bid/ask a changé significativement
                        bid_price_changed = False
                        ask_price_changed = False
                        
                        if state["bid_price_live"] is not None and state["bid_order_id"] is not None:
                            # Si le best bid a augmenté, on peut placer notre ordre plus haut
                            if best_bid > state["bid_price_live"] + tick:
                                bid_price_changed = True
                                print(f"[OPPORTUNITY] Best bid increased from {state['bid_price_live']} to {best_bid}")
                            # Si le best bid a baissé, on doit ajuster notre ordre
                            elif best_bid < state["bid_price_live"] - tick:
                                bid_price_changed = True
                                print(f"[OPPORTUNITY] Best bid decreased from {state['bid_price_live']} to {best_bid}")
                                
                        if state["ask_price_live"] is not None and state["ask_order_id"] is not None:
                            # Si le best ask a baissé, on peut placer notre ordre plus bas
                            if best_ask < state["ask_price_live"] - tick:
                                ask_price_changed = True
                                print(f"[OPPORTUNITY] Best ask decreased from {state['ask_price_live']} to {best_ask}")
                            # Si le best ask a augmenté, on doit ajuster notre ordre
                            elif best_ask > state["ask_price_live"] + tick:
                                ask_price_changed = True
                                print(f"[OPPORTUNITY] Best ask increased from {state['ask_price_live']} to {best_ask}")

                        # Vérifier les opportunités avec les 2ème ordres
                        if not our_bid_is_best and state["bid_price_live"] is not None and state["bid_order_id"] is not None:
                            # Si on peut être plus proche du 2ème bid
                            if second_bid > state["bid_price_live"] + tick:
                                bid_price_changed = True
                                print(f"[OPPORTUNITY] 2nd bid opportunity: {second_bid} vs our {state['bid_price_live']}")
                                
                        if not our_ask_is_best and state["ask_price_live"] is not None and state["ask_order_id"] is not None:
                            # Si on peut être plus proche du 2ème ask
                            if second_ask < state["ask_price_live"] - tick:
                                ask_price_changed = True
                                print(f"[OPPORTUNITY] 2nd ask opportunity: {second_ask} vs our {state['ask_price_live']}")

                        # Calculer les prix désirés - logique simplifiée
                        # Toujours se positionner le plus proche possible du marché
                        desired_bid_price = _round_to(best_bid + tick, price_prec)
                        desired_ask_price = _round_to(best_ask - tick, price_prec)

                        # Ajuster la stratégie selon notre position
                        if our_bid_is_best:
                            # Si on est le best bid, se positionner exactement à 1 tick du 2ème bid
                            desired_bid_price = _round_to(second_bid + tick, price_prec)
                            print(f"[STRATEGY] Positioning bid exactly 1 tick above 2nd bid: {desired_bid_price}")
                                
                        if our_ask_is_best:
                            # Si on est le best ask, se positionner exactement à 1 tick du 2ème ask
                            desired_ask_price = _round_to(second_ask - tick, price_prec)
                            print(f"[STRATEGY] Positioning ask exactly 1 tick below 2nd ask: {desired_ask_price}")

                        # Vérifier que le spread est valide
                        if desired_bid_price >= desired_ask_price:
                            desired_bid_price = _round_to(desired_ask_price - tick, price_prec)

                        print(f"[INFO] Desired bid: {desired_bid_price}, Desired ask: {desired_ask_price}")

                        # Calculer les distances pour voir à quel point on est proche
                        bid_distance = abs(desired_bid_price - best_bid)
                        ask_distance = abs(desired_ask_price - best_ask)
                        bid_distance_pct = (bid_distance / best_bid) * 100
                        ask_distance_pct = (ask_distance / best_ask) * 100
        
                        print(f"[DISTANCE] Bid distance: {bid_distance:.6f} ({bid_distance_pct:.3f}%)")
                        print(f"[DISTANCE] Ask distance: {ask_distance:.6f} ({ask_distance_pct:.3f}%)")
                        print(f"[SPREAD] Your spread: {((desired_ask_price - desired_bid_price) / desired_bid_price) * 100:.3f}%")
                        print("-" * 50)

                        # ─────────── Bid side ───────────
                        if not updating_bid:
                            # SAFETY CHECK: Verify we don't already have a bid order
                            if state["bid_order_id"] is None:
                                # Double-check with exchange
                                try:
                                    open_orders = await mexc.fetch_open_orders(symbol)
                                    our_bid_orders = [o for o in open_orders if o["side"] == "buy"]
                                    if our_bid_orders:
                                        print(f"[SAFETY] Found {len(our_bid_orders)} existing bid orders, skipping creation")
                                        await asyncio.sleep(CHECK_INTERVAL_SEC)
                                        continue
                                except Exception as e:
                                    print(f"[SAFETY] Error checking existing orders: {e}")
                                
                                updating_bid = True
                                qty = _round_to(max(min_amount, ORDER_VALUE_USDT_BUY / desired_bid_price), amount_prec)
                                print(f"[INFO] Creating bid order: {qty} @ {desired_bid_price}")
                                new_id, new_price = await _cancel_and_create(
                                    mexc, symbol, "buy", None, qty, desired_bid_price
                                )
                                if new_id:
                                    state["bid_order_id"] = new_id
                                    state["bid_price_live"] = new_price
                                    print(f"[INFO] Bid order created: {new_id}")
                                updating_bid = False
                            elif state["bid_price_live"] is not None and state["bid_order_id"] is not None:
                                # CRITICAL FIX: Verify the bid order still exists before updating
                                try:
                                    open_orders = await mexc.fetch_open_orders(symbol)
                                    bid_order_exists = any(o["id"] == state["bid_order_id"] for o in open_orders)
                                    
                                    if not bid_order_exists:
                                        print(f"[FIX] Bid order {state['bid_order_id']} no longer exists, clearing state")
                                        state["bid_order_id"] = None
                                        state["bid_price_live"] = None
                                        continue
                                        
                                except Exception as e:
                                    print(f"[WARN] Error verifying bid order existence: {e}")
                                    # Continue with update attempt if verification fails
                                
                                # Only update if order exists and price needs adjustment
                                if (abs(state["bid_price_live"] - desired_bid_price) >= tick or bid_price_changed):
                                    updating_bid = True
                                    qty = _round_to(max(min_amount, ORDER_VALUE_USDT_BUY / desired_bid_price), amount_prec)
                                    reason = "price change" if bid_price_changed else "spread adjustment"
                                    print(f"[INFO] Updating bid order ({reason}): {qty} @ {desired_bid_price}")
                                    new_id, new_price = await _cancel_and_create(
                                        mexc, symbol, "buy", state["bid_order_id"], qty, desired_bid_price
                                    )
                                    if new_id:
                                        state["bid_order_id"] = new_id
                                        state["bid_price_live"] = new_price
                                        print(f"[INFO] Bid order updated: {new_id}")
                                    updating_bid = False

                        # ─────────── Ask side ───────────
                        if not updating_ask:
                            # SAFETY CHECK: Verify we don't already have an ask order
                            if state["ask_order_id"] is None:
                                # Double-check with exchange
                                try:
                                    open_orders = await mexc.fetch_open_orders(symbol)
                                    our_ask_orders = [o for o in open_orders if o["side"] == "sell"]
                                    if our_ask_orders:
                                        print(f"[SAFETY] Found {len(our_ask_orders)} existing ask orders, skipping creation")
                                        await asyncio.sleep(CHECK_INTERVAL_SEC)
                                        continue
                                except Exception as e:
                                    print(f"[SAFETY] Error checking existing orders: {e}")
                                
                                updating_ask = True
                                qty = _round_to(max(min_amount, ORDER_VALUE_USDT_SELL / desired_ask_price), amount_prec)
                                print(f"[INFO] Creating ask order: {qty} @ {desired_ask_price}")
                                new_id, new_price = await _cancel_and_create(
                                    mexc, symbol, "sell", None, qty, desired_ask_price
                                )
                                if new_id:
                                    state["ask_order_id"] = new_id
                                    state["ask_price_live"] = new_price
                                    print(f"[INFO] Ask order created: {new_id}")
                                updating_ask = False
                            elif state["ask_price_live"] is not None and state["ask_order_id"] is not None:
                                # CRITICAL FIX: Verify the ask order still exists before updating
                                try:
                                    open_orders = await mexc.fetch_open_orders(symbol)
                                    ask_order_exists = any(o["id"] == state["ask_order_id"] for o in open_orders)
                                    
                                    if not ask_order_exists:
                                        print(f"[FIX] Ask order {state['ask_order_id']} no longer exists, clearing state")
                                        state["ask_order_id"] = None
                                        state["ask_price_live"] = None
                                        continue
                                        
                                except Exception as e:
                                    print(f"[WARN] Error verifying ask order existence: {e}")
                                    # Continue with update attempt if verification fails
                                
                                # Only update if order exists and price needs adjustment
                                if (abs(state["ask_price_live"] - desired_ask_price) >= tick or ask_price_changed):
                                    updating_ask = True
                                    qty = _round_to(max(min_amount, ORDER_VALUE_USDT_SELL / desired_ask_price), amount_prec)
                                    reason = "price change" if ask_price_changed else "spread adjustment"
                                    print(f"[INFO] Updating ask order ({reason}): {qty} @ {desired_ask_price}")
                                    new_id, new_price = await _cancel_and_create(
                                        mexc, symbol, "sell", state["ask_order_id"], qty, desired_ask_price
                                    )
                                    if new_id:
                                        state["ask_order_id"] = new_id
                                        state["ask_price_live"] = new_price
                                        print(f"[INFO] Ask order updated: {new_id}")
                                    updating_ask = False
                                
                    except Exception as e:
                        if shutdown_requested:
                            break
                        print(f"[ERROR] Book loop error: {e}")
                        print("[INFO] Waiting 5 seconds before retrying...")
                        await asyncio.sleep(5)
                    
                    await asyncio.sleep(CHECK_INTERVAL_SEC)

            try:
                # Run both loops concurrently
                tasks = [
                    asyncio.create_task(_book_loop()),
                    asyncio.create_task(_order_watcher(mexc, symbol, state))
                ]
                
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                if shutdown_requested:
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                            
            except asyncio.CancelledError:
                pass
            finally:
                print("[CLEANUP] Cancelling bot's own orders & closing connection…")
                for oid in filter(None, [state["bid_order_id"], state["ask_order_id"]]):
                    try:
                        await mexc.cancel_order(oid, symbol)
                        print(f"[CLEANUP] Cancelled order {oid}")
                    except Exception as e:
                        print(f"[CLEANUP] Could not cancel order {oid}: {e}")
                if mexc:
                    try:
                        await asyncio.wait_for(mexc.close(), timeout=5.0)
                    except asyncio.TimeoutError:
                        print("[WARN] Timeout closing connection")
                    except Exception as e:
                        print(f"[WARN] Error closing connection: {e}")
                
        except Exception as e:
            if shutdown_requested:
                break
            print(f"[ERROR] Main loop error: {e}")
            print("[INFO] Restarting bot in 10 seconds...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        asyncio.run(tighten_spread_rest())
    except KeyboardInterrupt:
        print("\n[EXIT] Interrupted by user")
        shutdown_requested = True
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}")
    finally:
        print("[CLEANUP] Bot stopped")
        shutdown_requested = True
