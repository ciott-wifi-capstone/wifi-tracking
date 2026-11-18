idf_component_register(
    SRCS "app_main.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_wifi esp_netif esp_event nvs_flash lwip esp_timer esp_hw_support
)
if(NOT DEFINED LAB_ANCHOR_NUMBER)
    set(LAB_ANCHOR_NUMBER 0)
endif()
if(NOT "${LAB_ANCHOR_NUMBER}" MATCHES "^[0-6]$")
    message(FATAL_ERROR "LAB_ANCHOR_NUMBER must be 0..6")
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE LAB_ANCHOR_NUMBER=${LAB_ANCHOR_NUMBER})
