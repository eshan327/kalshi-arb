class TwapCalculator:
    
    def __init__(self, strike_price: float, total_seconds: int = 60):
        self.strike_price = strike_price
        self.total_seconds = total_seconds
        self.observed_prices = []

    def add_price_tick(self, price: float):
        if self.seconds_elapsed() < self.total_seconds:
            self.observed_prices.append(price)

    def seconds_elapsed(self) -> int:
        return len(self.observed_prices)

    def seconds_remaining(self) -> int:
        return self.total_seconds - self.seconds_elapsed()

    def current_average(self) -> float:
        if not self.observed_prices:
            return 0.0
        return sum(self.observed_prices) / self.seconds_elapsed()

    def required_average(self) -> float:
        """
        Calculates the remaining average needed to hit the strike price.
        """

        rem_sec = self.seconds_remaining()
        if rem_sec == 0:
            return 0.0 # Window is closed

        target_sum = self.strike_price * self.total_seconds
        current_sum = sum(self.observed_prices)
        
        return (target_sum - current_sum) / rem_sec

    def is_outcome_deterministic(self, max_realistic_move: float = 500.0) -> str:
        """
        How certain/uncertain is the outcome right now?
        """
        if self.seconds_elapsed() == 0:
            return "UNCERTAIN"

        req_avg = self.required_average()
        current_spot = self.observed_prices[-1]

        # If required average is way above
        if req_avg > (current_spot + max_realistic_move):
            return "NO"  # Basically impossible to go above the strike
            
        # If required average is way lower
        if req_avg < (current_spot - max_realistic_move):
            return "YES" # Basically impossible to go below the strike

        return "UNCERTAIN"