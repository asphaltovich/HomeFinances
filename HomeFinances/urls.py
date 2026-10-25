from django.contrib import admin
from django.urls import path

from finance import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('register_page/', views.registration_view, name='register_page'),
    path('enter_site/', views.enter_site, name='enter_site'),
    path('confirmation/', views.confirmation, name='confirmation'),
    path('resend_code/', views.resend_code, name='resend_code'),
    path('main_page/', views.main_page, name='main_page'),
    path('expenses/', views.expenses, name='expenses'),
]