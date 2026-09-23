def subsidy(price: int, rate: float, cap: int) -> int:
    if price < 0 or not 0 <= rate <= 1 or cap < 0:
        raise ValueError("参数超出允许范围")
    return min(round(price * rate), cap)
