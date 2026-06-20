from app.middleware.rate_limiter import RateLimitMiddleware

# Exporta o middleware de barreira anti-DDoS e controle de tráfego
__all__ = ["RateLimitMiddleware"]
