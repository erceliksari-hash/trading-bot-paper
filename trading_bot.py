def paper_execute(symbol, meta, side):
    price = meta.get("price")
    atr = meta.get("atr")
    balance = WALLET.get("USD", 10000.0)
    risk_pct = CONFIG.get("daily_risk_pct", 0.01)
    qty = position_size(balance, risk_pct, price, atr)
    # Normalize base symbol
    base_symbol = symbol.split("/")[0] if "/" in symbol else symbol

    if side == "buy":
        cost = qty * price
        # Eğer bütçe yetmiyorsa, alım miktarını cüzdandaki USD ile sınırlayın
        if cost > balance:
            qty = balance / price if price > 0 else 0.0
            cost = qty * price
        if qty <= 0:
            logger.info("Buy skipped for %s: insufficient USD balance", symbol)
            return False
        WALLET["USD"] = WALLET.get("USD", 0.0) - cost
        WALLET[base_symbol] = WALLET.get(base_symbol, 0.0) + qty
    else:  # sell
        available = WALLET.get(base_symbol, 0.0)
        # Satılabilecek miktarı sınırlayın
        if available <= 0:
            logger.info("Sell skipped for %s: no holdings (%s)", symbol, base_symbol)
            return False
        sell_qty = min(qty, available)
        if sell_qty <= 0:
            logger.info("Sell skipped for %s: computed qty zero after limits", symbol)
            return False
        proceeds = sell_qty * price
        WALLET["USD"] = WALLET.get("USD", 0.0) + proceeds
        WALLET[base_symbol] = max(available - sell_qty, 0.0)
        qty = sell_qty
        cost = proceeds

    # Log ve persist
    log_trade(datetime.now(timezone.utc).isoformat(), symbol, side, price, qty, meta.get("reason", ""))
    with open("wallet.json", "w") as f:
        json.dump(WALLET, f, indent=2)
    try:
        send_telegram(f"Paper trade executed: {side} {symbol} price={price:.4f} qty={qty:.6f}\nSL={meta.get('sl')}\nTP={meta.get('tp')}\nReason: {meta.get('reason')}")
    except Exception:
        logger.exception("Failed to send telegram after paper trade")
    return True
